"""Per-question driver of the HolmesGPT public eval track (#666 PR-B).

Track 「HolmesGPT 公开评测集（竞品自评集）」. One attempt of one trial
(prereview H-7, §4 class 3)::

    ports free -> (280: wait past UTC midnight) -> after_test + purge (leftovers)
    -> before_test -> shift (fixed-time questions) -> PG, web, worker
    -> 120 s line -> POST /intake/ui -> 120 s line again -> wait for a terminal
    state -> stop worker -> freeze -> (include_tool_calls) all calls
    -> report text -> after_test + purge -> summary -> stop services

then a separate judging process (``judge_run``) reads the answer, the
summary gets the choice, and the completion marker is written last.

Credentials (H-5): the engineer's Elasticsearch key and the kind admin
kubeconfig reach only the setup/cleanup scripts and the shift client; the
worker gets the product's read-only ``es-ro`` identity and its model key; the
web gets neither; the judge key reaches only the judging process. A private
``--env-file`` is read in this process, never printed or written anywhere.

Usage (repository root)::

    .venv/bin/python -m scripts.holmes_evals.driver round --round <label> \\
        --upstream <holmesgpt checkout> --questions 254,275 --trials 1 \\
        --es fake --trace off --env-file <private KEY=value file>
    .venv/bin/python -m scripts.holmes_evals.driver swing ...   # trials 4-6
    .venv/bin/python -m scripts.holmes_evals.driver judge --round <label> --upstream <dir> --env-file <f>
    .venv/bin/python -m scripts.holmes_evals.driver scores --round <label>

Evidence ``docs/evidence/holmes-evals-<round>/<qid>/trial-<n>/summary.json``
and ``scores.json``; everything else under ``tmp/holmes-evals/<round>/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from scripts import lab_evidence, run_freeze
from scripts.holmes_evals import judge as judge_mod
from scripts.holmes_evals import scoring
from scripts.holmes_evals.intake import (
    TARGET_ID,
    SubmitResult,
    UiAccount,
    idempotency_key,
    new_account,
    submit_question,
    write_identities,
)
from scripts.holmes_evals.judge_run import JUDGE_KEYS, judge_env
from scripts.holmes_evals.questions import (
    B1_QUESTIONS,
    SKILL_ASSERTION_DROPPED,
    UPSTREAM_COMMIT,
    Question,
    UpstreamMismatch,
    judge_mode,
    load_question,
    question_dir,
    verify_upstream,
)
from scripts.holmes_evals.setup import (
    DEFAULT_SETUP_TIMEOUT,
    ScriptResult,
    purge_question_indices,
    run_script,
    setup_env,
)
from scripts.holmes_evals.shift import (
    LEAD,
    TIME_BOX_LIMITED,
    UTC_DATE_SENSITIVE,
    EsAdmin,
    ShiftError,
    ShiftRecord,
    apply_shift,
)
from scripts.holmes_evals.text import ToolCall, report_to_text
from scripts.holmes_evals.tool_calls import read_tool_calls
from scripts.itbench_lite import services
from scripts.itbench_lite.driver import (
    TERMINAL_WAIT,
    Cleanup,
    DeterministicError,
    DriverError,
    InfraFailure,
    RoundAbort,
    VoidAttempt,
    _pg_start,
    _pg_stop,
    run_state,
)
from scripts.itbench_lite.services import Started

ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = ROOT / "tmp" / "holmes-evals"
EVIDENCE_ROOT = ROOT / "docs" / "evidence"
EVIDENCE_PREFIX = "holmes-evals-"
SUMMARY_SCHEMA = "holmes-eval-trial-1"
CHANCES = 3  # L-D8 / H-18
VOID_SECONDS = 120  # H-24
MIDNIGHT_MARGIN = timedelta(minutes=10)
MIDNIGHT_AFTER_S = 5.0
MARKER = "complete.json"
JUDGE_INPUT = "judge-input.txt"
TOOL_CALLS_FILE = "tool-calls.json"
JUDGE_RESULT = "judge-result.json"
MAIN_TRIALS = scoring.MAIN_TRIALS
RERUN_TRIALS = scoring.RERUN_TRIALS
_ROUND = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")
MODEL_KEYS = ("DEEPSEEK_API_KEY",)
LANGSMITH_KEYS = ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID")
ENGINEER_KEY = "ELASTICSEARCH_API_KEY"
K8S_NAMESPACE = "otel-demo"
#: The judge's own key names; checked for in web/worker environments.
_JUDGE_SECRET_KEYS = ("ZAI_API_KEY", "GLM_API_KEY")
FINAL_JUDGE_ERRORS = frozenset({"JUDGE_REQUEST_OVERSIZE"})
ALWAYS_DISCLOSED = (
    "TOOL_FACE_FIXED",
    "NO_INTERMEDIATE_OUTPUT",
    "JUDGE_DIFFERS_FROM_UPSTREAM",
)


@dataclass(frozen=True)
class Ports:
    """Fixed ports (H-27, L-D3): the registry revision stays equal across
    attempts. ``prometheus``, ``jaeger_query`` and ``k8s`` are addresses where
    nothing listens (H-11)."""

    pg: int = 29210
    web: int = 29280
    es: int = 29292
    prometheus: int = 29290
    jaeger_query: int = 29286
    k8s: int = 29243

    def all(self) -> dict[str, int]:
        return asdict(self)

    def per_attempt(self) -> dict[str, int]:
        """Ports an attempt needs free: its own, and the unreachable ones."""
        return {k: v for k, v in self.all().items() if k != "es"}


@dataclass(frozen=True)
class K8sTarget:
    api_url: str
    ca_file: str
    token: str = field(repr=False)
    namespace: str = K8S_NAMESPACE


@dataclass
class Config:
    round: str
    upstream: Path
    questions: tuple[str, ...] = B1_QUESTIONS
    ports: Ports = field(default_factory=Ports)
    es_mode: Literal["fake", "external"] = "fake"
    es_url: str = ""
    es_readonly_env: Path | None = None
    k8s_env_file: Path | None = None
    kubeconfig: Path | None = None
    env_file: Path | None = None
    trace: Literal["off", "lab"] = "off"
    judge: bool = True
    allow_dirty: bool = False
    tmp_root: Path = TMP_ROOT
    evidence_root: Path = EVIDENCE_ROOT
    poll_seconds: float = 5.0
    terminal_wait: timedelta = TERMINAL_WAIT
    worker_settle_seconds: float = 5.0
    # Filled by prepare_round; never printed or frozen.
    git: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    engineer_key: str = field(default="", repr=False)
    product_es: dict[str, str] = field(default_factory=dict, repr=False)
    k8s: K8sTarget | None = None
    account: UiAccount | None = None
    project: dict[str, Any] | None = None

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
        return lab_evidence.lab_project(f"holmes-evals-{self.round}")

    @property
    def experiment(self) -> str:
        return f"holmes-evals-{self.round}"

    def trial_dir(self, qid: str, trial: int) -> Path:
        return self.round_dir / qid / f"trial-{trial}"

    def summary_path(self, qid: str, trial: int) -> Path:
        return self.evidence_dir / qid / f"trial-{trial}" / "summary.json"


# -- environments ----------------------------------------------------------------------


def load_env_file(path: Path | None) -> dict[str, str]:
    """``KEY=value`` lines of a private file; nothing is printed."""
    if path is None:
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if sep and key and value:
            values[key] = value
    return values


def unreachable_k8s(work_dir: Path, port: int) -> K8sTarget:
    """H-11 locally: a self-signed CA (L14) and an address nobody serves."""
    from scripts.itbench_lite.fake_k8s import generate_self_signed_cert

    cert, _ = generate_self_signed_cert(work_dir)
    return K8sTarget(
        api_url=f"https://127.0.0.1:{port}",
        ca_file=str(cert),
        token=secrets.token_urlsafe(24),
    )


def k8s_from_env_file(path: Path) -> K8sTarget:
    """The A1 read-only identity file (``OPSPILOT_K8S_*``) of a rental host."""
    values = load_env_file(path)
    try:
        return K8sTarget(
            api_url=values["OPSPILOT_K8S_API_URL"],
            ca_file=values["OPSPILOT_K8S_CA_FILE"],
            token=values["OPSPILOT_K8S_TOKEN"],
            namespace=values.get("OPSPILOT_K8S_NAMESPACE") or K8S_NAMESPACE,
        )
    except KeyError:
        raise RoundAbort("K8S_ENV_FILE_INCOMPLETE") from None


def _strs(env: Mapping[str, object]) -> dict[str, str]:
    """A child environment is str -> str (a ``Path`` value becomes its text)."""
    return {str(k): str(v) for k, v in env.items()}


def tool_env(cfg: Config) -> dict[str, str]:
    """The tool profile's addresses, the same for web and worker."""
    return {
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "OPSPILOT_OTEL_PROMETHEUS_URL": f"http://127.0.0.1:{cfg.ports.prometheus}",
        "OPSPILOT_OTEL_JAEGER_URL": f"http://127.0.0.1:{cfg.ports.jaeger_query}",
        "OPSPILOT_ES_URL": cfg.es_url,
        "OPSPILOT_HEALTH_BIND": "",
    }


def web_env(
    cfg: Config, pg: Any, identities: Path, account: UiAccount
) -> dict[str, str]:
    origin = f"http://127.0.0.1:{cfg.ports.web}"
    return _strs(
        services.base_env()
        | tool_env(cfg)
        | {
            "OPSPILOT_WEB_DSN": pg.web_dsn,
            "OPSPILOT_UI_USERS": account.users_env(),
            "OPSPILOT_ALLOWED_ORIGINS": origin,
            "OPSPILOT_AUTH_REVISION": "holmes-evals",
            "OPSPILOT_BIND": f"127.0.0.1:{cfg.ports.web}",
            "OPSPILOT_SELF_HEAL_INTERVAL_SECONDS": "0",
            "OPSPILOT_TARGET_IDENTITIES": str(identities),
        }
    )


def worker_env(
    cfg: Config,
    pg: Any,
    product_es: Mapping[str, str],
    k8s: K8sTarget,
    cluster_name: str | None,
    secrets: Mapping[str, str],
) -> dict[str, str]:
    env = (
        services.base_env()
        | tool_env(cfg)
        | {k: v for k, v in product_es.items() if k.startswith("OPSPILOT_ES_")}
        | {
            "OPSPILOT_ES_URL": cfg.es_url,
            "OPSPILOT_WORKER_DSN": pg.runtime_dsn,
            "OPSPILOT_K8S_API_URL": k8s.api_url,
            "OPSPILOT_K8S_CA_FILE": k8s.ca_file,
            "OPSPILOT_K8S_TOKEN": k8s.token,
            "OPSPILOT_K8S_NAMESPACE": k8s.namespace,
            "OPSPILOT_TRACE": cfg.trace,
            "OPSPILOT_WORKER_CAPACITY": "1",
        }
        | {k: secrets[k] for k in MODEL_KEYS if secrets.get(k)}
    )
    if cfg.trace == "lab":
        env.update({k: secrets[k] for k in LANGSMITH_KEYS if secrets.get(k)})
        if cfg.project_name:
            env["LANGSMITH_PROJECT"] = cfg.project_name
    if cluster_name:
        env["OPSPILOT_CLUSTER_NAME"] = cluster_name
    return _strs(env)


_WEB_FORBIDDEN = (
    "OPSPILOT_ES_API_KEY",
    "OPSPILOT_ES_USERNAME",
    "OPSPILOT_ES_PASSWORD",
    *MODEL_KEYS,
    *_JUDGE_SECRET_KEYS,
)


def check_env_isolation(
    env: Mapping[str, str], engineer_key: str, *, role: Literal["web", "worker"]
) -> None:
    """H-5: neither product process holds an engineer credential; the web
    holds no ES credential, model or judge key; the worker no judge key."""
    if any(k.startswith("ELASTICSEARCH_") for k in env):
        raise RoundAbort("ENV_ISOLATION", f"{role}:ELASTICSEARCH_*")
    if engineer_key and any(engineer_key in v for v in env.values()):
        raise RoundAbort("ENV_ISOLATION", f"{role}:engineer key value")
    forbidden = _WEB_FORBIDDEN if role == "web" else _JUDGE_SECRET_KEYS
    present = [k for k in forbidden if k in env]
    if present:
        raise RoundAbort("ENV_ISOLATION", f"{role}:{present[0]}")


def setup_to_delivery_ok(setup_ended: datetime, at: datetime) -> bool:
    return (at - setup_ended).total_seconds() <= VOID_SECONDS


def seconds_to_wait_for_utc_midnight(qid: str, now: datetime) -> float:
    """H-24: 280's index C carries today's UTC date; never set it up within
    ten minutes of UTC midnight."""
    if qid not in UTC_DATE_SENSITIVE:
        return 0.0
    now = now.astimezone(UTC)
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    left = midnight - now
    if left >= MIDNIGHT_MARGIN:
        return 0.0
    return left.total_seconds() + MIDNIGHT_AFTER_S


# -- host-touching defaults (Backends) ---------------------------------------------------------


def run_judge(job: Mapping[str, Any]) -> int:
    """The judging process; its output is discarded (answer-derived)."""
    argv = [
        sys.executable, "-m", "scripts.holmes_evals.judge_run",
        "--upstream", str(job["upstream"]),
        "--qid", str(job["qid"]),
        "--text-file", str(job["text_file"]),
        "--rationale-dir", str(job["rationale_dir"]),
        "--out", str(job["out"]),
    ]  # fmt: skip
    return subprocess.run(
        argv,
        cwd=ROOT,
        env=job["env"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode


def _private_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def start_fake_es(port: int, work_dir: Path) -> tuple[Started, str]:
    """The round's stand-in ES subprocess and its random engineer key."""
    key = secrets.token_urlsafe(32)
    key_file = work_dir / "fake-es-engineer.key"
    _private_file(key_file, key + "\n")
    proc = services.spawn(
        [sys.executable, "-m", "scripts.holmes_evals.fake_es",
         "--port", str(port), "--key-file", str(key_file)],
        services.base_env(),
        work_dir / "fake_es.log",
        stdout_pipe=True,
    )  # fmt: skip
    started = Started(
        "fake_es",
        lambda: services.stop_process(proc, 10),
        alive=lambda: proc.poll() is None,
    )
    try:
        line = json.loads(services._read_first_line(proc, 30))
        services.wait_tcp(port, timeout=30, alive=started.alive)
    except BaseException:
        started.stop()
        raise
    started.info = {"url": str(line["url"]), "pid": proc.pid}
    return started, key


def readonly_identity(es_url: str, engineer_key: str, out: Path) -> dict[str, str]:
    """``scripts/es_readonly_identity.py`` (B1-5) with the engineer key in
    its environment only; returns the product's ``OPSPILOT_ES_*`` values."""
    env = services.base_env() | {
        "ELASTICSEARCH_URL": es_url,
        "ELASTICSEARCH_API_KEY": engineer_key,
        "PYTHON_DOTENV_DISABLED": "1",
    }
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "es_readonly_identity.py"),
         "--backend", "elasticsearch", "--out", str(out), "--expiration", "1d"],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=120,
    )  # fmt: skip
    if done.returncode != 0 or not out.is_file():
        raise RoundAbort("ES_READONLY_IDENTITY_FAILED")
    return load_env_file(out)


@dataclass
class Backends:
    """Everything that touches the host; tests replace any of these."""

    start_pg: Callable[[Path, int], Any] = _pg_start
    stop_pg: Callable[[Any], None] = _pg_stop
    start_web: Callable[[Mapping[str, str], int, Path], Started] = services.start_web
    start_worker: Callable[..., Started] = services.start_worker
    run_script: Callable[..., ScriptResult] = run_script
    es_admin: Callable[[str, str], EsAdmin] = EsAdmin
    submit: Callable[..., SubmitResult] = submit_question
    run_state: Callable[[str, str], str | None] = run_state
    collect: Callable[[str, str], tuple[dict[str, Any], dict[str, Any]]] = (
        run_freeze.collect
    )
    read_tool_calls: Callable[[str, str], list[ToolCall]] = read_tool_calls
    trace_roots: Callable[[str | None, str, Mapping[str, str]], dict[str, Any]] = (
        lambda project, run_id, env: run_freeze.trace_roots(
            project, run_id, client_factory=lambda: lab_evidence.langsmith_client(env)
        )
    )
    judge: Callable[[Mapping[str, Any]], int] = run_judge
    port_in_use: Callable[[int], bool] = services.port_in_use
    start_fake_es: Callable[[int, Path], tuple[Started, str]] = start_fake_es
    readonly_identity: Callable[[str, str, Path], dict[str, str]] = readonly_identity
    unreachable_k8s: Callable[[Path, int], K8sTarget] = unreachable_k8s
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep


# -- one attempt ---------------------------------------------------------------------------------


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def disclosures(qid: str, question: Question, shift: ShiftRecord) -> list[str]:
    codes = list(ALWAYS_DISCLOSED)
    if shift.applied:
        codes += ["DESHIFT_TIME_ONLY_UNCHANGED", "DESHIFT_DATE_MIDPOINT"]
    if shift.question_rewrites:
        codes.append("QUESTION_TIME_REWRITTEN")
    if question.cluster_name:
        codes.append("CLUSTER_NAME_ENV_ONLY")
    if qid in SKILL_ASSERTION_DROPPED:
        codes.append("SKILL_ASSERTION_DROPPED")
    return codes


JUDGE_FIELDS = (
    "receiver",
    "model",
    "mode",
    "prompt_sha256",
    "choice",
    "passed",
    "error_code",
    "attempts",
    "request_bytes",
)


def unjudged_block(question: Question) -> dict[str, Any]:
    """The summary's ``judge`` block before judging: every key present, the
    judge's identity filled, the outcome fields null."""
    block: dict[str, Any] = dict.fromkeys(JUDGE_FIELDS)
    block.update(
        receiver="zai",
        model=judge_mod.JUDGE_MODEL,
        mode=judge_mode(question),
        prompt_sha256=judge_mod.HOLMES_EVAL_PROMPT_SHA256,
    )
    return block


def report_object(run_block: Mapping[str, Any]) -> tuple[Any, str | None]:
    """The ReportV2 the judge text comes from, or ``(None, reason)``.

    A report the product did not make available (``outcome.report_available``
    not true) counts as none (H-12, #645)."""
    from opspilot.investigation.reports import ReportV2

    report = run_block.get("report")
    outcome = run_block.get("outcome")
    available = (
        outcome.get("report_available") if isinstance(outcome, Mapping) else None
    )
    if not isinstance(report, Mapping):
        return None, "REPORT_REJECTED_BY_PRODUCT"
    if available is not True:
        return None, "REPORT_REJECTED_BY_PRODUCT"
    try:
        return ReportV2.model_validate(report), None
    except ValueError:
        return None, "REPORT_INVALID_FOR_CONVERSION"


@dataclass
class AttemptResult:
    run_id: str
    summary_path: Path
    text_path: Path | None


class Attempt:
    """One chance of one trial (see the module docstring)."""

    def __init__(
        self,
        cfg: Config,
        backends: Backends,
        qid: str,
        question: Question,
        trial: int,
        chance: int,
        history: list[dict[str, Any]],
    ) -> None:
        self.cfg, self.b, self.qid, self.q = cfg, backends, qid, question
        self.trial, self.chance, self.history = trial, chance, history
        self.dir = cfg.trial_dir(qid, trial) / f"chance-{chance}"
        self.cleanup = Cleanup()
        self.owned_ports: dict[str, int] = {}
        self._cleaned_after = False
        self._admin: EsAdmin | None = None
        self.block: dict[str, Any] = {
            "schema": SUMMARY_SCHEMA,
            "track": scoring.TRACK,
            "round": cfg.round,
            "qid": qid,
            "upstream_commit": UPSTREAM_COMMIT,
            "trial": trial,
            "chance": chance,
            "git": cfg.git,
            "ports": cfg.ports.all(),
            "cluster_name": {
                "value": question.cluster_name,
                "env_set": bool(question.cluster_name),
            },
            "setup": {},
            "time_box_limited": qid in TIME_BOX_LIMITED,
        }

    def _started(self, item: Started) -> Started:
        self.cleanup.push(item.name, item.stop)
        return item

    def _script(self, operation: str) -> ScriptResult:
        text = self.q.before_test if operation == "before_test" else self.q.after_test
        return self.b.run_script(
            text,
            operation=operation,
            cwd=question_dir(self.cfg.upstream, self.qid),
            env=setup_env(self.cfg.es_url, self.cfg.engineer_key, self.cfg.kubeconfig),
            timeout_s=self.q.setup_timeout or DEFAULT_SETUP_TIMEOUT,
            log_path=self.dir / f"{operation}-{len(self.block['setup'])}.log",
        )

    def _clean(self, slot: str) -> None:
        """``after_test`` (recorded, its exit code is upstream's business),
        then the evaluation's own purge, which fails closed: before the
        trial an ``InfraFailure`` (the chance is retried), after it
        ``RoundAbort("CLEANUP_FAILED")`` -- leftover data must never reach
        the next trial."""
        result = self._script("after_test")
        self.block["setup"][slot] = asdict(result)
        assert self._admin is not None
        phase = slot.split("_")[1]
        try:
            purged = purge_question_indices(self._admin, self.qid)
        except ShiftError as error:
            self.block["setup"].setdefault("purged", {})[phase] = [
                f"error:{error.code}"
            ]
            if phase == "before":
                raise InfraFailure("PURGE_FAILED", error.code) from None
            raise RoundAbort("CLEANUP_FAILED", f"purge:{error.code}") from None
        self.block["setup"].setdefault("purged", {})[phase] = purged

    def run(self) -> AttemptResult:
        try:
            return self._run()
        finally:
            failures = self.cleanup.close()
            if not self._cleaned_after and self._admin is not None:
                self._cleaned_after = True
                try:
                    self._clean("cleanup_after")
                except Exception:  # noqa: BLE001 - fail closed below
                    failures.append("cleanup_after")
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
        cfg, b, q = self.cfg, self.b, self.q
        self.dir.mkdir(parents=True, exist_ok=True)
        busy = [n for n, p in cfg.ports.per_attempt().items() if b.port_in_use(p)]
        if busy:
            raise InfraFailure("PORT_BUSY", ",".join(busy))
        self.owned_ports = {"pg": cfg.ports.pg, "web": cfg.ports.web}
        wait = seconds_to_wait_for_utc_midnight(self.qid, b.now())
        if wait:
            self.block["midnight_wait_seconds"] = round(wait, 1)
            b.sleep(wait)
        if not isinstance(q.user_prompt, str):
            raise DeterministicError("QUESTION_MULTI_TURN")
        prompt = q.user_prompt

        self._admin = b.es_admin(cfg.es_url, cfg.engineer_key)
        self._clean("cleanup_before")
        before = self._script("before_test")
        self.block["setup"]["before_test"] = asdict(before)
        if not before.ok:
            raise InfraFailure(
                "SETUP_TIMEOUT" if before.timed_out else "SETUP_FAILED",
                f"exit={before.exit_code}",
            )
        shift_started = b.now()
        self.block["setup"]["ended_at"] = _iso(shift_started)
        try:
            delivered, shift = apply_shift(self._admin, self.qid, prompt, shift_started)
        except ShiftError as error:
            if error.code in ("SHIFT_IO_FAILED", "ES_NOT_FOUND"):
                raise InfraFailure(error.code, error.detail) from None
            raise DeterministicError(error.code, error.detail) from None
        # The 120 s line runs from the end of the shift (184/185 rewrite
        # ~1600 indices); the shift's own duration is recorded beside it.
        setup_ended = b.now()
        self.block["shift"] = shift.as_json()
        self.block["shift_timing"] = {
            "started_at": _iso(shift_started),
            "ended_at": _iso(setup_ended),
            "seconds": round((setup_ended - shift_started).total_seconds(), 3),
        }
        self.block["question"] = {
            "sha256_original": _sha256(prompt),
            "sha256_delivered": _sha256(delivered),
            "rewrites": [
                {
                    "original": r.original,
                    "rewritten": r.rewritten,
                    "instant": _iso(r.instant),
                }
                for r in shift.question_rewrites
            ],
            "time_only_phrases": shift.time_only_phrases,
        }
        self.block["disclosures"] = disclosures(self.qid, q, shift)

        try:
            pg = b.start_pg(self.dir / "pg", cfg.ports.pg)
        except Exception as error:  # noqa: BLE001 - LabError codes
            raise InfraFailure(getattr(error, "code", "PG_START_FAILED")) from None
        self.cleanup.push("pg", lambda: b.stop_pg(pg))
        identities = self.dir / "target_identities.json"
        write_identities(identities)
        assert cfg.account is not None and cfg.k8s is not None
        wenv = web_env(cfg, pg, identities, cfg.account)
        check_env_isolation(wenv, cfg.engineer_key, role="web")
        kenv = worker_env(cfg, pg, cfg.product_es, cfg.k8s, q.cluster_name, cfg.secrets)
        check_env_isolation(kenv, cfg.engineer_key, role="worker")
        try:
            self._started(b.start_web(wenv, cfg.ports.web, self.dir / "web.log"))
            worker = self._started(
                b.start_worker(
                    kenv,
                    self.dir / "worker.log",
                    settle_seconds=cfg.worker_settle_seconds,
                )
            )
        except services.ServiceError as error:
            raise InfraFailure(error.code, str(error)) from None

        now = b.now()
        gap = (now - setup_ended).total_seconds()
        self.block["delivery"] = {
            "at": _iso(now),
            "setup_to_delivery_seconds": round(gap, 3),
        }
        if not setup_to_delivery_ok(setup_ended, now):
            raise VoidAttempt("SETUP_TO_DELIVERY_EXCEEDED", f"{gap:.1f}s")
        if shift.applied and shift.original_max is not None:
            # H-8's anchor rule at delivery time: the shifted data ends at
            # least LEAD before the question arrives.
            data_end = shift.original_max + timedelta(seconds=shift.seconds)
            lead = (now - data_end).total_seconds()
            self.block["delivery"]["data_end_to_delivery_seconds"] = round(lead, 3)
            if lead < LEAD.total_seconds():
                raise VoidAttempt("DATA_AFTER_DELIVERY", f"{lead:.1f}s")
        key = idempotency_key(cfg.round, self.qid, self.trial, self.chance)
        try:
            result = b.submit(
                f"http://127.0.0.1:{cfg.ports.web}",
                cfg.account,
                question=delivered,
                key=key,
                target_id=TARGET_ID,
            )
        except OSError as error:
            raise InfraFailure("INTAKE_UNREACHABLE", type(error).__name__) from None
        responded = b.now()
        self.block["delivery"].update(
            status=result.status,
            code=result.code,
            incident_id=result.incident_id,
            run_id=result.run_id,
            idempotency_label=key,
            responded_at=_iso(responded),
        )
        if result.code in ("TARGET_UNRESOLVED", "TARGET_AMBIGUOUS", "UNKNOWN_TARGET"):
            raise RoundAbort("TARGET_UNRESOLVED", str(result.code))
        if result.status != 201 or not result.run_id:
            raise InfraFailure("INTAKE_REFUSED", f"{result.status} {result.code}")
        if not setup_to_delivery_ok(setup_ended, responded):
            raise VoidAttempt("SETUP_TO_DELIVERY_EXCEEDED_AFTER_POST")
        run_id = result.run_id

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
        worker.stop()

        run_block, ledger = b.collect(pg.owner_dsn, run_id)
        calls: list[ToolCall] | None = None
        trial_dir = self.cfg.trial_dir(self.qid, self.trial)
        conversion: dict[str, Any] = {"with_tool_calls": False, "tool_calls": None}
        if q.include_tool_calls:
            calls = b.read_tool_calls(pg.owner_dsn, run_id)
            raw_calls = json.dumps(
                [asdict(c) for c in calls], ensure_ascii=False, indent=1, default=str
            )
            (self.dir / TOOL_CALLS_FILE).write_text(raw_calls + "\n")
            conversion.update(
                tool_calls=len(calls), tool_calls_sha256=_sha256(raw_calls)
            )
        report, reason = report_object(run_block)
        converted = report_to_text(report, calls, shift.as_text_shift())
        text_path: Path | None = None
        conversion.update(
            with_tool_calls=converted.with_tool_calls,
            deshift_count=converted.deshift_count,
            reason=reason if converted.text is None else None,
            text_sha256=None if converted.text is None else _sha256(converted.text),
        )
        self.block["conversion"] = conversion

        self._clean("cleanup_after")
        self._cleaned_after = True
        self.block["history"] = list(self.history)
        self.block["judge"] = unjudged_block(q)
        self.block["category"] = "ok" if converted.text is not None else "no_report"
        trace = b.trace_roots(cfg.project_name, run_id, cfg.secrets)
        summary = {
            "schema": SUMMARY_SCHEMA,
            "status": "completed",
            "run": run_block,
            "trace": trace,
            "holmes_eval": self.block,
        }
        # Text first, summary second, marker last (after judging).
        for stale in (JUDGE_INPUT, JUDGE_RESULT, MARKER):
            (trial_dir / stale).unlink(missing_ok=True)
        if converted.text is not None:
            text_path = trial_dir / JUDGE_INPUT
            text_path.write_text(converted.text, encoding="utf-8")
        _, summary_path = lab_evidence.freeze(
            ledger,
            experiment=cfg.experiment,
            run_id=run_id,
            evidence_dir=cfg.summary_path(self.qid, self.trial).parent,
            summary=summary,
        )
        return AttemptResult(run_id, summary_path, text_path)


# -- trials, resume, judging -----------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    os.replace(partial, path)


def _judge_final(judge: Any) -> bool:
    if not isinstance(judge, Mapping):
        return False
    return (
        judge.get("choice") in ("A", "B")
        or judge.get("error_code") in FINAL_JUDGE_ERRORS
    )


def trial_state(
    cfg: Config, qid: str, trial: int
) -> Literal["done", "missing", "judge", "rerun"]:
    summary = _read_json(cfg.summary_path(qid, trial))
    if not isinstance(summary, Mapping):
        return "rerun"
    status = summary.get("status")
    if status == "missing" and summary.get("reason"):
        return "missing"
    if status != "completed":
        return "rerun"
    block = summary.get("holmes_eval") or {}
    marker = _read_json(cfg.trial_dir(qid, trial) / MARKER)
    no_report = block.get("category") == "no_report"
    if (
        isinstance(marker, Mapping)
        and marker.get("status") == "completed"
        and (no_report or _judge_final(block.get("judge")))
    ):
        return "done"
    if no_report or (cfg.trial_dir(qid, trial) / JUDGE_INPUT).is_file():
        return "judge"
    return "rerun"


def _set_aside(cfg: Config, qid: str, trial: int) -> None:
    """Leftovers of an unfinished trial go to ``trial-N/stale/<stamp>/``;
    nothing is deleted (ITBench #632)."""
    evidence = cfg.summary_path(qid, trial).parent
    trial_dir = cfg.trial_dir(qid, trial)
    leftovers = (
        sorted(p for p in trial_dir.iterdir() if p.name != "stale")
        if trial_dir.is_dir()
        else []
    )
    if not evidence.exists() and not leftovers:
        return
    target = trial_dir / "stale" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target.mkdir(parents=True, exist_ok=False)
    for path in leftovers:
        shutil.move(str(path), target / path.name)
    if evidence.exists():
        shutil.move(str(evidence), target / "evidence")


def finish_judging(
    cfg: Config, backends: Backends, qid: str, trial: int
) -> dict[str, Any]:
    """Judge a frozen trial; write the choice into its summary, then the
    marker when the judgement is final."""
    path = cfg.summary_path(qid, trial)
    summary = _read_json(path)
    if not isinstance(summary, dict):
        return {"status": "rerun"}
    block = summary.setdefault("holmes_eval", {})
    trial_dir = cfg.trial_dir(qid, trial)
    if block.get("category") == "no_report":
        _write_json(
            trial_dir / MARKER, {"status": "completed", "category": "no_report"}
        )
        return {"status": "completed", "category": "no_report"}
    text_file = trial_dir / JUDGE_INPUT
    if not cfg.judge or not text_file.is_file():
        return {"status": "judge_pending"}
    out = trial_dir / JUDGE_RESULT
    out.unlink(missing_ok=True)
    exit_code = backends.judge(
        {
            "upstream": cfg.upstream,
            "qid": qid,
            "text_file": text_file,
            "rationale_dir": trial_dir / "answer-derived",
            "out": out,
            "env": judge_env({**services.base_env(), **cfg.secrets}),
        }
    )
    result = _read_json(out) if exit_code == 0 else None
    question = load_question(cfg.upstream, qid)
    judged: dict[str, Any] = {
        "receiver": "zai",
        "model": judge_mod.JUDGE_MODEL,
        "mode": judge_mode(question),
        "prompt_sha256": judge_mod.HOLMES_EVAL_PROMPT_SHA256,
        "choice": None,
        "passed": False,
        "error_code": "JUDGE_PROCESS_FAILED" if result is None else None,
        "attempts": 0,
        "request_bytes": None,
    }
    if isinstance(result, Mapping):
        for name in (
            "choice",
            "passed",
            "error_code",
            "attempts",
            "request_bytes",
            "prompt_sha256",
        ):
            judged[name] = result.get(name)
        judged["passed"] = result.get("choice") == "A"
    block["judge"] = judged
    block["category"] = "ok" if judged["choice"] in ("A", "B") else "judge_exhausted"
    _write_json(path, summary)
    if _judge_final(judged):
        _write_json(
            trial_dir / MARKER, {"status": "completed", "category": block["category"]}
        )
    return {"status": "completed", "category": block["category"], "choice": judged["choice"],
            "error_code": judged["error_code"]}  # fmt: skip


def _missing(
    cfg: Config, qid: str, trial: int, reason: str, history: list[dict[str, Any]]
) -> None:
    path = cfg.summary_path(qid, trial)
    _write_json(
        path,
        {
            "schema": SUMMARY_SCHEMA,
            "status": "missing",
            "reason": reason,
            "frozen_at": datetime.now(UTC).isoformat(),
            "holmes_eval": {
                "schema": SUMMARY_SCHEMA,
                "qid": qid,
                "trial": trial,
                "history": history,
                "git": cfg.git,
                "category": "env_broken",
                "time_box_limited": qid in TIME_BOX_LIMITED,
            },
        },
    )


def run_trial(
    cfg: Config, backends: Backends, qid: str, question: Question, trial: int
) -> dict[str, Any]:
    """Up to ``CHANCES`` attempts; voids and infrastructure failures retry."""
    history: list[dict[str, Any]] = []
    for chance in range(1, CHANCES + 1):
        attempt = Attempt(cfg, backends, qid, question, trial, chance, history)
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
        record: dict[str, Any] = {
            "status": "completed",
            "run_id": result.run_id,
            "history": history,
        }
        record["judge"] = finish_judging(cfg, backends, qid, trial)
        return record
    _missing(cfg, qid, trial, "CHANCES_EXHAUSTED", history)
    return {"status": "missing", "history": history}


def run_question(
    cfg: Config, backends: Backends, qid: str, trials: Sequence[int]
) -> dict[str, Any]:
    states = {t: trial_state(cfg, qid, t) for t in trials}
    records: dict[int, Any] = {}
    for t, state in states.items():
        if state == "judge":
            records[t] = {"resumed": "judge", **finish_judging(cfg, backends, qid, t)}
    pending = [t for t, s in states.items() if s == "rerun"]
    skipped = [t for t, s in states.items() if s in ("done", "missing")]
    if not pending:
        return {"qid": qid, "trials": records, "skipped": skipped}
    question = load_question(cfg.upstream, qid)
    for t in pending:
        _set_aside(cfg, qid, t)
    for t in pending:
        try:
            records[t] = run_trial(cfg, backends, qid, question, t)
        except DeterministicError as error:
            for left in [x for x in pending if x not in records]:
                _missing(
                    cfg,
                    qid,
                    left,
                    error.code,
                    [{"kind": "deterministic", "code": error.code}],
                )
                records[left] = {"status": "missing", "code": error.code}
            break
    return {"qid": qid, "trials": records, "skipped": skipped}


def trial_record(summary: Mapping[str, Any]) -> scoring.Trial:
    block = summary.get("holmes_eval") or {}
    qid, index = str(block["qid"]), int(block["trial"])
    raw_judge = block.get("judge")
    judge: Mapping[str, Any] = raw_judge if isinstance(raw_judge, Mapping) else {}
    category: scoring.Category
    if summary.get("status") == "missing":
        category = "env_broken"
    elif block.get("category") == "no_report":
        category = "no_report"
    elif judge.get("choice") in ("A", "B"):
        category = "ok"
    else:
        category = "judge_exhausted"
    return scoring.Trial(
        qid=qid,
        index=index,
        passed=category == "ok" and judge.get("choice") == "A",
        category=category,
        time_box_limited=qid in TIME_BOX_LIMITED,
    )


def round_trials(cfg: Config) -> list[scoring.Trial]:
    trials = []
    for path in sorted(cfg.evidence_dir.glob("*/trial-*/summary.json")):
        summary = _read_json(path)
        if isinstance(summary, Mapping) and isinstance(
            summary.get("holmes_eval"), Mapping
        ):
            trials.append(trial_record(summary))
    return trials


# -- round -------------------------------------------------------------------------------------------


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


def resolve_questions(spec: str | None) -> tuple[str, ...]:
    """``254,275`` or full directory names; only B1 questions."""
    if not spec:
        return B1_QUESTIONS
    out = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        found = [q for q in B1_QUESTIONS if q == item or q.split("_", 1)[0] == item]
        if len(found) != 1:
            raise RoundAbort("QUESTION_UNKNOWN", item)
        out.append(found[0])
    return tuple(dict.fromkeys(out))


class Round:
    """Round-level resources (stand-in ES); ``close`` stops them."""

    def __init__(self) -> None:
        self.cleanup = Cleanup()

    def close(self) -> list[str]:
        return self.cleanup.close()


def prepare_round(cfg: Config, backends: Backends, round_: Round) -> None:
    if not _ROUND.match(cfg.round):
        raise RoundAbort("BAD_ROUND", cfg.round)
    try:
        verify_upstream(cfg.upstream)
    except UpstreamMismatch as error:
        raise RoundAbort(error.code) from None
    cfg.git = git_state(cfg.allow_dirty)
    private = load_env_file(cfg.env_file)
    for name in (*MODEL_KEYS, *LANGSMITH_KEYS, *JUDGE_KEYS, ENGINEER_KEY):
        if name not in private and os.environ.get(name):
            private[name] = os.environ[name]
    cfg.secrets = {
        k: v
        for k, v in private.items()
        if k in (*MODEL_KEYS, *LANGSMITH_KEYS, *JUDGE_KEYS)
    }
    if not cfg.secrets.get("DEEPSEEK_API_KEY"):
        raise RoundAbort("DEEPSEEK_API_KEY_MISSING")
    if cfg.judge and not any(cfg.secrets.get(k) for k in _JUDGE_SECRET_KEYS):
        raise RoundAbort("JUDGE_KEY_MISSING")
    if cfg.trace == "lab":
        if not cfg.secrets.get("LANGSMITH_API_KEY"):
            raise RoundAbort("LANGSMITH_API_KEY_MISSING")
        client = lab_evidence.langsmith_client(cfg.secrets)
        project = cfg.project_name
        assert project is not None
        cfg.project = lab_evidence.ensure_longlived_project(client, project)
    cfg.round_dir.mkdir(parents=True, exist_ok=True)
    if cfg.es_mode == "fake":
        if backends.port_in_use(cfg.ports.es):
            raise RoundAbort("PORT_BUSY", "es")
        try:
            started, key = backends.start_fake_es(
                cfg.ports.es, cfg.round_dir / "fake-es"
            )
        except services.ServiceError as error:
            raise RoundAbort("FAKE_ES_START_FAILED", error.code) from None
        round_.cleanup.push(started.name, started.stop)
        cfg.es_url = str(started.info["url"])
        cfg.engineer_key = key
        cfg.product_es = backends.readonly_identity(
            cfg.es_url, key, cfg.round_dir / "es-readonly.env"
        )
    else:
        if not cfg.es_url:
            raise RoundAbort("ES_URL_MISSING")
        cfg.engineer_key = private.get(ENGINEER_KEY, "")
        if not cfg.engineer_key:
            raise RoundAbort("ELASTICSEARCH_API_KEY_MISSING")
        if cfg.es_readonly_env is not None:
            cfg.product_es = load_env_file(cfg.es_readonly_env)
        else:
            cfg.product_es = backends.readonly_identity(
                cfg.es_url, cfg.engineer_key, cfg.round_dir / "es-readonly.env"
            )
    if cfg.product_es.get("OPSPILOT_ES_URL", cfg.es_url).rstrip(
        "/"
    ) != cfg.es_url.rstrip("/"):
        raise RoundAbort("ES_URL_MISMATCH")
    if not (
        cfg.product_es.get("OPSPILOT_ES_API_KEY")
        or cfg.product_es.get("OPSPILOT_ES_USERNAME")
    ):
        raise RoundAbort("ES_READONLY_IDENTITY_FAILED")
    cfg.k8s = (
        k8s_from_env_file(cfg.k8s_env_file)
        if cfg.k8s_env_file is not None
        else backends.unreachable_k8s(cfg.round_dir / "k8s-ca", cfg.ports.k8s)
    )
    cfg.account = new_account()


def run_round(
    cfg: Config, backends: Backends, trials: Sequence[int], *, swing_only: bool = False
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    questions: Sequence[str] = cfg.questions
    if swing_only:
        table = scoring.score(
            [t for t in round_trials(cfg) if t.qid in set(cfg.questions)], cfg.questions
        )
        questions = [q for q in cfg.questions if q in set(table["swing_questions"])]
    for qid in questions:
        report = run_question(cfg, backends, qid, trials)
        reports.append(report)
        print(json.dumps(report, default=str), flush=True)
    return reports


# -- CLI -----------------------------------------------------------------------------------------------


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--round", required=True)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument(
        "--questions", help="comma-separated numbers or names; default all 30"
    )
    parser.add_argument("--es", choices=("fake", "external"), default="fake")
    parser.add_argument("--es-url", default="")
    parser.add_argument("--es-readonly-env", type=Path)
    parser.add_argument("--k8s-env-file", type=Path)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument(
        "--env-file", type=Path, help="private KEY=value file (never printed)"
    )
    parser.add_argument("--trace", choices=("off", "lab"), default="off")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--tmp-root", type=Path, default=TMP_ROOT)
    parser.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    for name, default in Ports().all().items():
        parser.add_argument(
            f"--{name.replace('_', '-')}-port", type=int, default=default
        )


def _config(args: argparse.Namespace) -> Config:
    return Config(
        round=args.round,
        upstream=args.upstream.resolve(),
        questions=resolve_questions(args.questions),
        ports=Ports(**{n: getattr(args, f"{n}_port") for n in Ports().all()}),
        es_mode=args.es,
        es_url=args.es_url,
        es_readonly_env=args.es_readonly_env,
        k8s_env_file=args.k8s_env_file,
        kubeconfig=args.kubeconfig,
        env_file=args.env_file,
        trace=args.trace,
        judge=not args.no_judge,
        allow_dirty=args.allow_dirty,
        tmp_root=args.tmp_root.resolve(),
        evidence_root=args.evidence_root.resolve(),
    )


def _abort(error: DriverError) -> int:
    print(json.dumps({"status": "aborted", "code": error.code, "detail": error.detail}))
    return 2


def main(argv: Sequence[str] | None = None, backends: Backends | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    many = sub.add_parser("round", help="each question, trials 1..N")
    _common(many)
    many.add_argument("--trials", type=int, default=3)
    swing = sub.add_parser("swing", help="trials 4-6 of the swinging questions")
    _common(swing)
    again = sub.add_parser("judge", help="judge frozen trials not judged yet")
    again.add_argument("--round", required=True)
    again.add_argument("--upstream", required=True, type=Path)
    again.add_argument("--env-file", type=Path)
    again.add_argument("--tmp-root", type=Path, default=TMP_ROOT)
    again.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    table = sub.add_parser("scores", help="scores.json of a round")
    table.add_argument("--round", required=True)
    table.add_argument("--questions")
    table.add_argument("--out", type=Path)
    table.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    args = parser.parse_args(argv)
    backends = backends or Backends()

    try:
        if args.command == "scores":
            cfg = Config(
                round=args.round,
                upstream=Path(),
                questions=resolve_questions(args.questions),
                evidence_root=args.evidence_root.resolve(),
            )
            known = set(cfg.questions)
            result = scoring.score(
                [t for t in round_trials(cfg) if t.qid in known], cfg.questions
            )
            out = args.out or cfg.evidence_dir / "scores.json"
            _write_json(out, result)
            print(json.dumps({"out": str(out), "main": result["main"]}))
            return 0
        if args.command == "judge":
            cfg = Config(
                round=args.round,
                upstream=args.upstream.resolve(),
                env_file=args.env_file,
                tmp_root=args.tmp_root.resolve(),
                evidence_root=args.evidence_root.resolve(),
            )
            try:
                verify_upstream(cfg.upstream)
            except UpstreamMismatch as error:
                raise RoundAbort(error.code) from None
            private = load_env_file(cfg.env_file)
            cfg.secrets = {
                k: private.get(k) or os.environ.get(k, "")
                for k in JUDGE_KEYS
                if private.get(k) or os.environ.get(k)
            }
            if not any(cfg.secrets.get(k) for k in _JUDGE_SECRET_KEYS):
                raise RoundAbort("JUDGE_KEY_MISSING")
            results = []
            for path in sorted(cfg.evidence_dir.glob("*/trial-*/summary.json")):
                qid, trial = (
                    path.parent.parent.name,
                    int(path.parent.name.split("-")[1]),
                )
                if trial_state(cfg, qid, trial) == "judge":
                    results.append(
                        {
                            "qid": qid,
                            "trial": trial,
                            **finish_judging(cfg, backends, qid, trial),
                        }
                    )
            print(json.dumps(results, indent=1))
            return (
                0
                if all(
                    r.get("choice") in ("A", "B") or r.get("category") == "no_report"
                    for r in results
                )
                else 1
            )
        if args.command == "round" and not 1 <= args.trials <= len(MAIN_TRIALS):
            raise RoundAbort("BAD_TRIALS", str(args.trials))
        cfg = _config(args)
        round_ = Round()
        try:
            prepare_round(cfg, backends, round_)
            trials = (
                list(RERUN_TRIALS)
                if args.command == "swing"
                else list(range(1, args.trials + 1))
            )
            reports = run_round(
                cfg, backends, trials, swing_only=args.command == "swing"
            )
        finally:
            failures = round_.close()
            if failures:
                raise RoundAbort("CLEANUP_FAILED", ",".join(failures))
        pending = [
            (r["qid"], t)
            for r in reports
            for t in r.get("trials", {})
            if trial_state(cfg, r["qid"], t) == "judge"
        ]
        return 1 if pending else 0
    except RoundAbort as error:
        return _abort(error)


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "Attempt",
    "AttemptResult",
    "Backends",
    "Config",
    "K8sTarget",
    "Ports",
    "Round",
    "check_env_isolation",
    "finish_judging",
    "k8s_from_env_file",
    "load_env_file",
    "prepare_round",
    "resolve_questions",
    "run_question",
    "run_trial",
    "seconds_to_wait_for_utc_midnight",
    "setup_to_delivery_ok",
    "tool_env",
    "trial_record",
    "trial_state",
    "unreachable_k8s",
    "web_env",
    "worker_env",
]
