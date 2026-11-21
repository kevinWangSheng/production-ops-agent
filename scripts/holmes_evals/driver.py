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
With ``--es external`` and no ``--es-readonly-env``, the read-only key is
created with the admin's ``ELASTICSEARCH_USERNAME``/``ELASTICSEARCH_PASSWORD``
(basic auth; Elasticsearch 8 refuses to derive it from an API key); they
reach only that one setup subprocess.

Usage (repository root)::

    .venv/bin/python -m scripts.holmes_evals.driver round --round <label> \\
        --upstream <holmesgpt checkout> --questions 254,275 --trials 1 \\
        --es fake --trace off --env-file <private KEY=value file>
    .venv/bin/python -m scripts.holmes_evals.driver swing ...   # trials 4-6
    .venv/bin/python -m scripts.holmes_evals.driver judge --round <label> --upstream <dir> --env-file <f>
    .venv/bin/python -m scripts.holmes_evals.driver scores --round <label>

Evidence ``docs/evidence/holmes-evals-<round>/<qid>/trial-<n>/summary.json``
and ``scores.json``; everything else under ``tmp/holmes-evals/<round>/``.

C5 questions (PR-C, ``--questions c5``): each Loki question brings its own
Loki and each attempt gets a fresh VictoriaLogs; the product only ever holds
the read-only gateway's account (C5-U1 = A; ``--loki gateway`` /
``--victorialogs docker`` on a rental host, ``fake`` stand-ins locally). One
attempt adds, around the B1 flow: the question's ports free -> (100a, 101,
259_loki: wait for a whole-day offset) -> per-attempt VictoriaLogs or stand-in
-> after_test + release check -> before_test -> ``kubectl port-forward`` per
``port_forwards`` -> gateway switch -> whole-day Loki shift -> ... -> stop the
port-forwards -> after_test + release check (namespace gone, ports free).
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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from scripts import lab_evidence, run_freeze
from scripts.holmes_evals import db_backends as db_mod
from scripts.holmes_evals import judge as judge_mod
from scripts.holmes_evals import knowledge as knowledge_mod
from scripts.holmes_evals import scoring, task_list
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
from scripts.holmes_evals.log_backends import (
    KNOWLEDGE_QUESTIONS,
    LOG_QUESTIONS,
    VICTORIALOGS_IMAGE,
    LogQuestionSpec,
    backend_of,
    namespace_gone,
    namespaces_with_prefix,
    start_fake_log_backend,
    start_gateway,
    start_port_forward,
    start_victorialogs,
    switch_gateway,
)
from scripts.holmes_evals.log_backends import RELEASE_WAIT_S as RELEASE_WAIT_S
from scripts.holmes_evals.questions import (
    ALL_QUESTIONS,
    B1_QUESTIONS,
    C1_QUESTIONS,
    QUESTION_GROUPS,
    R8_13_SKILL_ASSERTION_DROPPED,
    TODO_HARD_QUESTIONS,
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
    log_setup_env,
    purge_question_indices,
    run_script,
    setup_env,
)
from scripts.holmes_evals.shift import (
    LEAD,
    LOKI_FIXED_TIME,
    TIME_BOX_LIMITED,
    UTC_DATE_SENSITIVE,
    EsAdmin,
    LokiAdmin,
    ShiftError,
    ShiftRecord,
    apply_shift,
    seconds_to_wait_for_shift_day,
    shift_question,
)
from scripts.holmes_evals.task_list import (
    TODO_MODES,
    task_list_stats_from_rows,
    todo_for,
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
#: PR-E: the C1-U2 comparison table's questions.
TODO_COMPARED: tuple[str, ...] = C1_QUESTIONS + TODO_HARD_QUESTIONS
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
#: ``--es external``: the admin's basic credentials, used only to create the
#: product's read-only key (real Elasticsearch 8 refuses to derive a key with
#: privileges from an API key). Never stored on ``Config``.
ADMIN_USER = "ELASTICSEARCH_USERNAME"
ADMIN_PASSWORD = "ELASTICSEARCH_PASSWORD"
K8S_NAMESPACE = "otel-demo"
#: The judge's own key names; checked for in web/worker environments.
_JUDGE_SECRET_KEYS = ("ZAI_API_KEY", "GLM_API_KEY")
FINAL_JUDGE_ERRORS = frozenset({"JUDGE_REQUEST_OVERSIZE"})
#: PR-C: shift failures an attempt retries (the rest are deterministic).
INFRA_SHIFT_ERRORS = frozenset(
    {
        "SHIFT_IO_FAILED",
        "ES_NOT_FOUND",
        "SHIFT_NO_FIXED_ENTRIES",
        "SHIFT_DAY_UNAVAILABLE",
    }
)
#: 258 asks about "the last 5 minutes": data written (``before_test`` start)
#: to delivery at most this long, leaving a minute for the first query.
DATA_WINDOW_SECONDS = {"258_victorialogs_streams": 240.0}
LOG_ENV_PREFIXES = ("OPSPILOT_LOKI_", "OPSPILOT_VICTORIALOGS_")
_LOG_SECRET_SUFFIXES = ("_USERNAME", "_PASSWORD", "_BEARER_TOKEN")
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
    # PR-C: the product's gateway ports (round-level, or the stand-in's
    # gateway side), the Loki stand-in's backend side, the attempt's
    # VictoriaLogs.
    loki_gateway: int = 29231
    loki_fake: int = 29232
    victorialogs_gateway: int = 29233
    victorialogs: int = 29428

    def all(self) -> dict[str, int]:
        return asdict(self)

    def per_attempt(self) -> dict[str, int]:
        """Ports an attempt needs free: its own, and the unreachable ones."""
        return {k: v for k, v in self.all().items() if k not in _ROUND_PORTS}

    def for_summary(self, backend: str) -> dict[str, int]:
        """The ``ports`` block: B1 keeps its six keys (PR-C adds only
        ``data_plane`` to a B1 summary); C5 questions list every port."""
        if backend == "elasticsearch":
            return {k: v for k, v in self.all().items() if k not in _LOG_PORTS}
        return self.all()

    def log_ports(self, backend: str, mode: str) -> dict[str, int]:
        """The ports one attempt of a C5 question opens itself."""
        if backend == "loki" and mode == "fake":
            return {"loki_fake": self.loki_fake, "loki_gateway": self.loki_gateway}
        if backend == "victorialogs":
            out = {"victorialogs": self.victorialogs}
            if mode == "fake":
                out["victorialogs_gateway"] = self.victorialogs_gateway
            return out
        return {}


_LOG_PORTS = frozenset(
    {"loki_gateway", "loki_fake", "victorialogs_gateway", "victorialogs"}
)
_ROUND_PORTS = _LOG_PORTS | {"es"}


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
    loki_mode: Literal["fake", "gateway"] = "fake"
    victorialogs_mode: Literal["fake", "docker"] = "fake"
    #: PR-E: ``question`` follows each question's ``enable_todo``; ``on``/
    #: ``off`` force ``OPSPILOT_TODO_WRITE`` for the C1-U2 comparison rounds.
    todo_mode: Literal["question", "on", "off"] = "question"
    # Filled by prepare_round; never printed or frozen.
    git: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    engineer_key: str = field(default="", repr=False)
    product_es: dict[str, str] = field(default_factory=dict, repr=False)
    product_loki: dict[str, str] = field(default_factory=dict, repr=False)
    product_victorialogs: dict[str, str] = field(default_factory=dict, repr=False)
    k8s: K8sTarget | None = None
    account: UiAccount | None = None
    #: PR-D: the round's runbook import account (C2 questions only).
    import_account: UiAccount | None = None
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

    def log_mode(self, backend: str) -> str:
        if backend == "loki":
            return self.loki_mode
        if backend == "victorialogs":
            return self.victorialogs_mode
        if backend in ("kubernetes", "sql", "mongodb"):
            return "none"
        return self.es_mode

    def gateway_state(self, backend: str) -> Path:
        return self.round_dir / f"{backend}-gateway"


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


def product_log_url(cfg: Config, backend: str) -> str:
    """The product's log backend address: always the gateway port (C5-U1)."""
    port = (
        cfg.ports.loki_gateway if backend == "loki" else cfg.ports.victorialogs_gateway
    )
    return f"http://127.0.0.1:{port}"


def tool_env(
    cfg: Config,
    backend: str = "elasticsearch",
    *,
    knowledge: bool = False,
    todo: bool = False,
    db_env: Mapping[str, str] | None = None,
    cluster_name: str | None = None,
) -> dict[str, str]:
    """The tool profile's addresses, the same for web and worker. Only the
    question's own log backend is configured (as upstream's per-question
    ``toolsets.yaml``); B1's default is unchanged. PR-D: a ``kubernetes``
    question has no log backend; ``knowledge`` sets
    ``OPSPILOT_KNOWLEDGE_TOOL=1`` (web and worker both, C2b)."""
    env = {
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "OPSPILOT_OTEL_PROMETHEUS_URL": f"http://127.0.0.1:{cfg.ports.prometheus}",
        "OPSPILOT_OTEL_JAEGER_URL": f"http://127.0.0.1:{cfg.ports.jaeger_query}",
        "OPSPILOT_ES_URL": cfg.es_url,
        "OPSPILOT_HEALTH_BIND": "",
    }
    if backend == "loki":
        del env["OPSPILOT_ES_URL"]
        env["OPSPILOT_LOKI_URL"] = product_log_url(cfg, backend)
    elif backend == "victorialogs":
        del env["OPSPILOT_ES_URL"]
        env["OPSPILOT_VICTORIALOGS_URL"] = product_log_url(cfg, backend)
    elif backend == "kubernetes":
        del env["OPSPILOT_ES_URL"]
    elif backend in ("sql", "mongodb"):
        # PR-F (C6): the address only (web and worker); the account is the
        # worker's (``worker_env``).
        del env["OPSPILOT_ES_URL"]
        name = "OPSPILOT_MONGODB_URL" if backend == "mongodb" else "OPSPILOT_SQL_URL"
        env[name] = (db_env or {})[name]
    if knowledge:
        env["OPSPILOT_KNOWLEDGE_TOOL"] = "1"
    if todo:
        # PR-E: the task list (C1); web and worker both, or the claim is
        # refused on differing versions.
        env["OPSPILOT_TODO_WRITE"] = "1"
    if cluster_name:
        # #722: picks the prompt variant, so web and worker both (or the
        # claim is refused on differing prompt revisions).
        env["OPSPILOT_CLUSTER_NAME"] = cluster_name
    return env


def web_env(
    cfg: Config,
    pg: Any,
    identities: Path,
    account: UiAccount,
    *,
    backend: str = "elasticsearch",
    knowledge: bool = False,
    import_account: UiAccount | None = None,
    todo: bool = False,
    db_env: Mapping[str, str] | None = None,
    cluster_name: str | None = None,
) -> dict[str, str]:
    origin = f"http://127.0.0.1:{cfg.ports.web}"
    users = account.users_env()
    if import_account is not None:
        # PR-D: the runbook import account, apart from the asking one.
        users += "," + import_account.users_env()
    return _strs(
        services.base_env()
        | tool_env(
            cfg,
            backend,
            knowledge=knowledge,
            todo=todo,
            db_env=db_env,
            cluster_name=cluster_name,
        )
        | {
            "OPSPILOT_WEB_DSN": pg.web_dsn,
            "OPSPILOT_UI_USERS": users,
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
    *,
    backend: str = "elasticsearch",
    log_env: Mapping[str, str] | None = None,
    knowledge: bool = False,
    todo: bool = False,
    db_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    data_plane: dict[str, str]
    if backend == "kubernetes":
        data_plane = {}
    elif backend in ("sql", "mongodb"):
        # PR-F (C6): the attempt's read-only account (``db_backends``).
        prefix = "OPSPILOT_MONGODB_" if backend == "mongodb" else "OPSPILOT_SQL_"
        data_plane = {k: v for k, v in (db_env or {}).items() if k.startswith(prefix)}
    elif backend == "elasticsearch":
        data_plane = {
            k: v for k, v in product_es.items() if k.startswith("OPSPILOT_ES_")
        } | {"OPSPILOT_ES_URL": cfg.es_url}
    else:
        prefix = "OPSPILOT_LOKI_" if backend == "loki" else "OPSPILOT_VICTORIALOGS_"
        # The account's own address wins, so ``check_env_isolation`` sees an
        # address that points past the gateway.
        data_plane = {prefix + "URL": product_log_url(cfg, backend)} | {
            k: v for k, v in (log_env or {}).items() if k.startswith(prefix)
        }
    env = (
        services.base_env()
        | tool_env(
            cfg,
            backend,
            knowledge=knowledge,
            todo=todo,
            db_env=db_env,
            cluster_name=cluster_name,
        )
        | data_plane
        | {
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
    return _strs(env)


_WEB_FORBIDDEN = (
    "OPSPILOT_ES_API_KEY",
    "OPSPILOT_ES_USERNAME",
    "OPSPILOT_ES_PASSWORD",
    *MODEL_KEYS,
    *_JUDGE_SECRET_KEYS,
)


def check_env_isolation(
    env: Mapping[str, str],
    engineer_key: str,
    *,
    role: Literal["web", "worker"],
    backend_addresses: Sequence[str] = (),
    db_port: int | None = None,
) -> None:
    """H-5: neither product process holds an engineer credential; the web
    holds no ES or log-backend credential, model or judge key; the worker no
    judge key. PR-C: neither holds the engineer's ``VICTORIALOGS_URL``, and
    the product's Loki/VictoriaLogs address is never a backend's own port
    (``backend_addresses``, ``host:port``) -- only the gateway's."""
    if any(k.startswith("ELASTICSEARCH_") for k in env):
        raise RoundAbort("ENV_ISOLATION", f"{role}:ELASTICSEARCH_*")
    if "VICTORIALOGS_URL" in env:
        raise RoundAbort("ENV_ISOLATION", f"{role}:VICTORIALOGS_URL")
    for name in ("OPSPILOT_LOKI_URL", "OPSPILOT_VICTORIALOGS_URL"):
        value = env.get(name, "")
        if value and any(
            value.rstrip("/").endswith("//" + address) for address in backend_addresses
        ):
            raise RoundAbort("ENV_ISOLATION", f"{role}:{name} not the gateway")
    if role == "web":
        leaked = [
            k
            for k in env
            if k.startswith(LOG_ENV_PREFIXES) and k.endswith(_LOG_SECRET_SUFFIXES)
        ]
        if leaked:
            raise RoundAbort("ENV_ISOLATION", f"web:{leaked[0]}")
    # PR-F (C6): the read-only account only in the worker; no question admin
    # variable anywhere; the product's address is the question's
    # port-forward.
    if role == "web":
        leaked_db = [k for k in env if k in _DB_SECRET_KEYS]
        if leaked_db:
            raise RoundAbort("ENV_ISOLATION", f"web:{leaked_db[0]}")
    admin = [k for k in db_mod.ADMIN_ENV_NAMES if k in env]
    if admin:
        raise RoundAbort("ENV_ISOLATION", f"{role}:{admin[0]}")
    if db_port is not None:
        for name in ("OPSPILOT_SQL_URL", "OPSPILOT_MONGODB_URL"):
            value = env.get(name)
            if value is not None and _url_port(value) != db_port:
                raise RoundAbort("ENV_ISOLATION", f"{role}:{name} not the port-forward")
    if engineer_key and any(engineer_key in v for v in env.values()):
        raise RoundAbort("ENV_ISOLATION", f"{role}:engineer key value")
    forbidden = _WEB_FORBIDDEN if role == "web" else _JUDGE_SECRET_KEYS
    present = [k for k in forbidden if k in env]
    if present:
        raise RoundAbort("ENV_ISOLATION", f"{role}:{present[0]}")


_DB_SECRET_KEYS = (
    "OPSPILOT_SQL_USERNAME",
    "OPSPILOT_SQL_PASSWORD",
    "OPSPILOT_MONGODB_USERNAME",
    "OPSPILOT_MONGODB_PASSWORD",
)


def _url_port(value: str) -> int | None:
    import urllib.parse

    try:
        return urllib.parse.urlsplit(value).port
    except ValueError:
        return None


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


def readonly_identity(
    es_url: str,
    engineer_key: str,
    out: Path,
    *,
    admin: tuple[str, str] | None = None,
) -> dict[str, str]:
    """``scripts/es_readonly_identity.py`` (B1-5) with the creating credential
    in its environment only; returns the product's ``OPSPILOT_ES_*`` values.

    ``admin`` (user, password) authenticates with basic auth instead of
    ``engineer_key``: a real cluster refuses an API key creating a key with
    role descriptors; the stand-in ES accepts only ``ApiKey``."""
    credential = (
        {"ELASTICSEARCH_USERNAME": admin[0], "ELASTICSEARCH_PASSWORD": admin[1]}
        if admin is not None
        else {"ELASTICSEARCH_API_KEY": engineer_key}
    )
    env = services.base_env() | {
        "ELASTICSEARCH_URL": es_url,
        **credential,
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
    readonly_identity: Callable[..., dict[str, str]] = readonly_identity
    unreachable_k8s: Callable[[Path, int], K8sTarget] = unreachable_k8s
    # PR-C (C5 log backends).
    start_gateway: Callable[..., tuple[Started, dict[str, str]]] = start_gateway
    switch_gateway: Callable[..., None] = switch_gateway
    start_victorialogs: Callable[[int, Path], Started] = start_victorialogs
    start_fake_log_backend: Callable[..., tuple[Started, dict[str, str]]] = (
        start_fake_log_backend
    )
    start_port_forward: Callable[..., Started] = start_port_forward
    loki_admin: Callable[[str], LokiAdmin] = LokiAdmin
    namespace_gone: Callable[[str, Mapping[str, str]], bool] = namespace_gone
    #: PR-E: the namespaces with a prefix, None when kubectl did not answer.
    namespaces_with_prefix: Callable[[str, Mapping[str, str]], list[str] | None] = (
        namespaces_with_prefix
    )
    # PR-D (C2 knowledge fixtures).
    import_runbooks: Callable[..., tuple[list[dict[str, Any]], list[str]]] = (
        knowledge_mod.import_runbooks
    )
    revoke_runbooks: Callable[..., list[str]] = knowledge_mod.revoke_runbooks
    active_runbooks: Callable[[str, str], list[str]] = knowledge_mod.active_runbooks
    run_catalog: Callable[[str, str], dict[str, Any] | None] = knowledge_mod.run_catalog
    # PR-E (C1 task list).
    run_tool_names: Callable[[str, str], list[str]] = task_list.run_tool_names
    task_list_stats: Callable[[str, str], dict[str, Any]] = task_list.task_list_stats
    # PR-F (C6 databases).
    provision_db_account: Callable[..., None] = db_mod.provision_db_account
    db_precheck: Callable[..., str | None] = db_mod.db_precheck
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep


# -- one attempt ---------------------------------------------------------------------------------


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def disclosures(
    qid: str,
    question: Question,
    shift: ShiftRecord,
    *,
    k8s_namespace: str | None = None,
) -> list[str]:
    codes = list(ALWAYS_DISCLOSED)
    if shift.applied and shift.backend == "loki":
        # Whole-day offset: times of day and lone dates need no deshift.
        codes += [
            "SHIFT_WHOLE_DAYS",
            "LOKI_ORIGINALS_KEPT",
            "LOKI_FUTURE_DROPPED",
            "LOG_LINE_TIME_REWRITTEN",
        ]
    elif shift.applied:
        codes += ["DESHIFT_TIME_ONLY_UNCHANGED", "DESHIFT_DATE_MIDPOINT"]
    if shift.question_rewrites:
        codes.append("QUESTION_TIME_REWRITTEN")
    if question.cluster_name:
        codes.append("CLUSTER_NAME_ENV_ONLY")
    if qid in R8_13_SKILL_ASSERTION_DROPPED:
        codes.append("SKILL_ASSERTION_DROPPED")
    spec = LOG_QUESTIONS.get(qid)
    if spec is not None and spec.upstream_k8s_disabled:
        codes.append("K8S_TOOLS_UPSTREAM_DISABLED")
    if (
        spec is not None
        and spec.namespace is not None
        and k8s_namespace is not None
        and spec.namespace != k8s_namespace
    ):
        codes.append("K8S_SCOPE_DIFFERS")
    elif (
        spec is not None
        and spec.namespace_prefix is not None
        and k8s_namespace is not None
        and not k8s_namespace.startswith(spec.namespace_prefix)
    ):
        # PR-E (283): its ``app-283-*`` namespaces, the same rule.
        codes.append("K8S_SCOPE_DIFFERS")
    if qid in db_mod.DB_QUESTIONS:
        # PR-F: OpsPilot's read-only account against upstream's admin, and
        # the tool face difference (R8-4, R8-15, R8-16).
        codes += ["DB_READ_ONLY_ACCOUNT", "DB_TOOL_SURFACE_DIFFERS"]
        if db_mod.DB_QUESTIONS[qid].dialect == "mssql":
            codes.append("MSSQL_NO_TLS")
    if qid in KNOWLEDGE_QUESTIONS:
        codes.append("RUNBOOKS_IMPORTED_REVIEWED")
        if qid in GLOBAL_INSTRUCTIONS_QUESTIONS:
            codes.append("GLOBAL_INSTRUCTIONS_NOT_IMPORTED")
    return codes


#: PR-D: C2 questions whose directory holds ``global_instructions.json``,
#: which is not imported (no OpsPilot channel for global instructions).
GLOBAL_INSTRUCTIONS_QUESTIONS = frozenset({"162_get_skills"})


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
        self.forwards = Cleanup()
        self.owned_ports: dict[str, int] = {}
        self._cleaned_after = False
        self._clean_armed = False
        self._admin: EsAdmin | None = None
        self.backend = backend_of(qid)
        self.spec: LogQuestionSpec | None = LOG_QUESTIONS.get(qid)
        self.mode = cfg.log_mode(self.backend)
        self.log_env: dict[str, str] = {}
        # PR-D: this attempt's knowledge fixtures (C2 questions only).
        self.knowledge = qid in KNOWLEDGE_QUESTIONS
        # PR-E: this attempt's task-list switch (C1).
        self.todo, self.todo_source = todo_for(cfg.todo_mode, question)
        # The ``todo`` summary block: the comparison questions (the main
        # run's "off" rows come from it) and any attempt with the switch on
        # or forced; other summaries keep their keys.
        self.todo_recorded = _todo_block(cfg, qid, question) is not None
        # PR-F: this attempt's database question and read-only account.
        self.db: db_mod.DbQuestionSpec | None = db_mod.DB_QUESTIONS.get(qid)
        self.db_env: dict[str, str] = {}
        self._imported: list[dict[str, Any]] = []
        self._owner_dsn: str | None = None
        self._revoked = False
        self.block: dict[str, Any] = {
            "schema": SUMMARY_SCHEMA,
            "track": scoring.TRACK,
            "round": cfg.round,
            "qid": qid,
            "upstream_commit": UPSTREAM_COMMIT,
            "trial": trial,
            "chance": chance,
            "git": cfg.git,
            "ports": cfg.ports.for_summary(backend_of(qid)),
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

    def _setup_env(self) -> dict[str, str]:
        if self.spec is None:
            return setup_env(
                self.cfg.es_url, self.cfg.engineer_key, self.cfg.kubeconfig
            )
        return log_setup_env(
            self.spec,
            victorialogs_url=f"http://127.0.0.1:{self.cfg.ports.victorialogs}",
            kubeconfig=self.cfg.kubeconfig,
        )

    def _loki_address(self) -> str:
        """The question's own Loki (engineer side, gateway upstream)."""
        if self.mode == "fake":
            return f"127.0.0.1:{self.cfg.ports.loki_fake}"
        assert self.spec is not None and self.spec.address is not None
        return self.spec.address

    def _backend_addresses(self) -> list[str]:
        ports = self.cfg.ports
        out = [f"127.0.0.1:{ports.loki_fake}", f"127.0.0.1:{ports.victorialogs}"]
        if self.spec is not None and self.spec.address:
            out.append(self.spec.address)
        return out

    def _script(self, operation: str) -> ScriptResult:
        text = self.q.before_test if operation == "before_test" else self.q.after_test
        return self.b.run_script(
            text,
            operation=operation,
            cwd=question_dir(self.cfg.upstream, self.qid),
            env=self._setup_env(),
            timeout_s=self.q.setup_timeout or DEFAULT_SETUP_TIMEOUT,
            log_path=self.dir / f"{operation}-{len(self.block['setup'])}.log",
        )

    def _release_check(self) -> dict[str, Any]:
        """C5: the question's namespace gone (waits up to ``RELEASE_WAIT_S``)
        and its host ports free."""
        assert self.spec is not None
        state: dict[str, Any] = {"namespace": self.spec.namespace}
        deadline = self.b.now() + timedelta(seconds=RELEASE_WAIT_S)
        gone = True
        if self.spec.namespace is not None:
            env = self._setup_env()
            while not (gone := self.b.namespace_gone(self.spec.namespace, env)):
                if self.b.now() >= deadline:
                    break
                self.b.sleep(2)
        if gone and self.spec.namespace_prefix is not None:
            # PR-E (283): every ``app-283-*`` namespace gone; an unanswered
            # kubectl (None) is not released.
            env = self._setup_env()
            while True:
                left = self.b.namespaces_with_prefix(self.spec.namespace_prefix, env)
                gone = left == []
                if gone or self.b.now() >= deadline:
                    break
                self.b.sleep(2)
            # The summary is public evidence: only how many are left (None:
            # kubectl did not answer); the names stay in the attempt's local
            # directory (git-ignored ``tmp/``).
            state["namespaces_left"] = None if left is None else len(left)
            if left:
                self.dir.mkdir(parents=True, exist_ok=True)
                with (self.dir / "namespaces-left.log").open("a") as log:
                    log.write(" ".join(left) + "\n")
        state["namespace_gone"] = gone
        busy = [p for p in self.spec.host_ports if self.b.port_in_use(p)]
        state["busy_ports"] = busy
        state["ok"] = gone and not busy
        return state

    def _clean(self, slot: str) -> None:
        """``after_test`` (recorded, its exit code is upstream's business),
        then the evaluation's own purge, which fails closed: before the
        trial an ``InfraFailure`` (the chance is retried), after it
        ``RoundAbort("CLEANUP_FAILED")`` -- leftover data must never reach
        the next trial."""
        result = self._script("after_test")
        self.block["setup"][slot] = asdict(result)
        phase = slot.split("_")[1]
        if self.spec is not None:
            released = self._release_check()
            self.block["setup"].setdefault("released", {})[phase] = released
            self.block["setup"].setdefault("purged", {})[phase] = []
            if not released["ok"]:
                if phase == "before":
                    raise InfraFailure("LOG_BACKEND_NOT_RELEASED", json.dumps(released))
                raise RoundAbort("CLEANUP_FAILED", "log backend not released")
            return
        assert self._admin is not None
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

    # -- PR-D: knowledge fixtures --------------------------------------------------------

    def _knowledge_block(self) -> dict[str, Any]:
        block: dict[str, Any] = self.block.setdefault("knowledge", {})
        return block

    def _import_knowledge(self, owner_dsn: str) -> None:
        """Import and approve the question's fixtures through the workbench,
        failing closed on residue, refusals and a mismatched result."""
        cfg, b = self.cfg, self.b
        assert cfg.import_account is not None
        block = self._knowledge_block()
        directory = question_dir(cfg.upstream, self.qid)
        fixtures = knowledge_mod.fixture_rows(directory)
        block["fixtures"] = fixtures
        block["import_account_prefix"] = knowledge_mod.IMPORT_ACCOUNT_PREFIX
        block["catalog_retries"] = sum(
            1 for h in self.history if h.get("code") == "RUNBOOK_CATALOG_MISMATCH"
        )
        if not fixtures:
            raise DeterministicError("RUNBOOK_FIXTURES_MISSING")
        before = b.active_runbooks(owner_dsn, TARGET_ID)
        block["active_before_import"] = before
        if before:
            raise RoundAbort("RUNBOOK_RESIDUE", ",".join(before))
        rows, refusals = b.import_runbooks(
            f"http://127.0.0.1:{cfg.ports.web}",
            cfg.import_account,
            directory,
            TARGET_ID,
        )
        self._imported = list(rows)
        block["imported"] = [
            {k: row.get(k) for k in ("name", "entry_id", "revision")} for row in rows
        ]
        if refusals:
            raise InfraFailure("RUNBOOK_IMPORT_FAILED", refusals[0])
        after = b.active_runbooks(owner_dsn, TARGET_ID)
        block["active_after_import"] = after
        if sorted(after) != sorted(f["name"] for f in fixtures):
            raise InfraFailure("RUNBOOK_IMPORT_MISMATCH", ",".join(after))

    def _check_run_catalog(self, owner_dsn: str, run_id: str) -> None:
        catalog = self.b.run_catalog(owner_dsn, run_id)
        self._knowledge_block()["run_catalog"] = knowledge_mod.catalog_summary(catalog)
        names = [str(row.get("name")) for row in self._imported]
        if not knowledge_mod.catalog_matches(catalog, names):
            # The model was not shown the intended list: not a scorable
            # attempt (revoked below by the ``run`` fallback).
            raise InfraFailure(
                "RUNBOOK_CATALOG_MISMATCH",
                json.dumps(knowledge_mod.catalog_summary(catalog)),
            )

    def _revoke_knowledge(self, *, after_run: bool) -> None:
        """Revoke what this attempt imported and check nothing stays active.
        After a Run either failure aborts the round; on the fallback path
        (the attempt failed earlier) it is recorded only -- the attempt's PG
        is discarded and the next attempt checks for residue first."""
        if self._revoked or not self._imported or self._owner_dsn is None:
            return
        self._revoked = True
        cfg, b = self.cfg, self.b
        assert cfg.import_account is not None
        block = self._knowledge_block()
        try:
            refusals = b.revoke_runbooks(
                f"http://127.0.0.1:{cfg.ports.web}", cfg.import_account, self._imported
            )
            block["revoked"] = len(self._imported) - len(refusals)
            left = b.active_runbooks(self._owner_dsn, TARGET_ID)
        except Exception as error:  # noqa: BLE001 - classified below
            block["revoke_error"] = type(error).__name__
            if after_run:
                raise RoundAbort("CLEANUP_FAILED", "runbook revoke") from None
            return
        block["active_after_revoke"] = left
        if not after_run:
            return
        if refusals:
            raise RoundAbort("CLEANUP_FAILED", "runbook revoke")
        if left:
            raise RoundAbort("CLEANUP_FAILED", "runbook residue")

    def run(self) -> AttemptResult:
        try:
            return self._run()
        finally:
            if self.knowledge and not self._revoked:
                # Before the web stops (``cleanup`` below).
                self._revoke_knowledge(after_run=False)
            failures = self.forwards.close() + self.cleanup.close()
            if not self._cleaned_after and self._clean_armed:
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

    def _start_log_backend(self) -> None:
        """The attempt's own VictoriaLogs, or the stand-in backend; the
        product's log account (``self.log_env``)."""
        cfg, b = self.cfg, self.b
        if self.spec is None or self.backend not in ("loki", "victorialogs"):
            return
        work = self.dir / "log-backend"
        try:
            if self.mode == "fake":
                started, self.log_env = b.start_fake_log_backend(
                    self.backend,
                    cfg.ports.loki_fake
                    if self.backend == "loki"
                    else cfg.ports.victorialogs,
                    cfg.ports.loki_gateway
                    if self.backend == "loki"
                    else cfg.ports.victorialogs_gateway,
                    work,
                )
                self._started(started)
                return
        except services.ServiceError as error:
            raise InfraFailure("FAKE_LOG_BACKEND_START_FAILED", error.code) from None
        if self.backend == "victorialogs":
            try:
                self._started(b.start_victorialogs(cfg.ports.victorialogs, work))
            except services.ServiceError as error:
                raise InfraFailure("VICTORIALOGS_START_FAILED", error.code) from None
            self.log_env = dict(cfg.product_victorialogs)
        else:
            self.log_env = dict(cfg.product_loki)

    def _connect_log_backend(self) -> None:
        """After ``before_test``: the question's port-forwards and the Loki
        gateway's switch to the question's Loki (rental mode only)."""
        if self.spec is None:
            return
        env = self._setup_env()
        for i, forward in enumerate(self.q.port_forwards or []):
            try:
                started = self.b.start_port_forward(
                    forward, env, self.dir / f"port-forward-{i}.log"
                )
            except services.ServiceError as error:
                raise InfraFailure("PORT_FORWARD_FAILED", error.code) from None
            self.forwards.push(started.name, started.stop)
        if self.backend == "loki" and self.mode == "gateway":
            try:
                self.b.switch_gateway(
                    "loki", self._loki_address(), self.cfg.gateway_state("loki")
                )
            except (services.ServiceError, OSError) as error:
                raise InfraFailure(
                    "GATEWAY_SWITCH_FAILED",
                    getattr(error, "code", type(error).__name__),
                ) from None

    def _provision_db(self) -> None:
        """PR-F: the attempt's read-only account (C6-21) in the question's
        container, then the product's own account check over it."""
        db = self.db
        assert db is not None
        password = secrets.token_urlsafe(24)
        started = time.monotonic()
        try:
            self.b.provision_db_account(db, self._setup_env(), password)
        except db_mod.DbSetupError as error:
            raise InfraFailure(error.code, error.detail) from None
        env = db_mod.product_env(db, password)
        try:
            problem = db_mod.precheck_with_wait(
                db, env, self.b.db_precheck, sleep=self.b.sleep
            )
        except db_mod.DbLoginFailed as error:
            raise InfraFailure("DB_LOGIN_FAILED", str(error)) from None
        code = "pass" if problem is None else problem.split(":", 1)[0]
        self.block["db_account"] = db_mod.summary_block(
            db, code, time.monotonic() - started
        )
        if problem is not None:
            # The grant script and the product's whitelist disagree: a retry
            # gives the same answer.
            raise DeterministicError("DB_ACCOUNT_NOT_READ_ONLY", problem)
        self.db_env = env

    def _data_plane(self) -> dict[str, Any]:
        cfg = self.cfg
        if self.db is not None:
            return {
                "backend": self.db.backend,
                "dialect": self.db.dialect,
                "mode": "port-forward",
                "product_url": db_mod.product_url(self.db),
                "gateway_upstream": None,
                "port_forwards": [
                    {
                        k: f.get(k)
                        for k in ("namespace", "service", "local_port", "remote_port")
                    }
                    for f in (self.q.port_forwards or [])
                ],
                "tls": db_mod.tls_mode(self.db),
                "victorialogs_image": None,
            }
        if self.spec is None:
            return {
                "backend": "elasticsearch",
                "mode": cfg.es_mode,
                "product_url": cfg.es_url,
                "gateway_upstream": None,
                "port_forwards": [],
                "victorialogs_image": None,
            }
        if self.backend == "kubernetes":
            return {
                "backend": "kubernetes",
                "mode": "none",
                "product_url": None,
                "gateway_upstream": None,
                "port_forwards": [],
                "victorialogs_image": None,
            }
        return {
            "backend": self.backend,
            "mode": self.mode,
            "product_url": product_log_url(cfg, self.backend),
            "gateway_upstream": (
                self._loki_address()
                if self.backend == "loki"
                else f"127.0.0.1:{cfg.ports.victorialogs}"
            ),
            "port_forwards": [
                {
                    k: f.get(k)
                    for k in ("namespace", "service", "local_port", "remote_port")
                }
                for f in (self.q.port_forwards or [])
            ],
            "victorialogs_image": VICTORIALOGS_IMAGE
            if self.backend == "victorialogs" and self.mode == "docker"
            else None,
        }

    def _run(self) -> AttemptResult:
        cfg, b, q = self.cfg, self.b, self.q
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.backend in ("sql", "mongodb"):
            # PR-F: re-read here, the table may differ from construction time.
            self.db = db_mod.DB_QUESTIONS.get(self.qid)
            if self.db is None:
                raise DeterministicError("DB_QUESTION_UNKNOWN", self.qid)
        own = cfg.ports.log_ports(self.backend, self.mode)
        busy = [
            n for n, p in {**cfg.ports.per_attempt(), **own}.items() if b.port_in_use(p)
        ]
        if self.spec is not None:
            busy += [f"question:{p}" for p in self.spec.host_ports if b.port_in_use(p)]
        if busy:
            raise InfraFailure("PORT_BUSY", ",".join(busy))
        if self.db is not None and self.db.local_port in (cfg.ports.pg, cfg.ports.web):
            # C6-8: the question's database is never OpsPilot's own.
            raise RoundAbort("DB_PORT_CONFLICT", str(self.db.local_port))
        self.owned_ports = {"pg": cfg.ports.pg, "web": cfg.ports.web, **own}
        wait = seconds_to_wait_for_utc_midnight(self.qid, b.now())
        if wait:
            self.block["midnight_wait_seconds"] = round(wait, 1)
            b.sleep(wait)
        if not isinstance(q.user_prompt, str):
            raise DeterministicError("QUESTION_MULTI_TURN")
        prompt = q.user_prompt
        timeout_s = q.setup_timeout or DEFAULT_SETUP_TIMEOUT
        if self.qid in LOKI_FIXED_TIME:
            day_wait = seconds_to_wait_for_shift_day(self.qid, b.now(), timeout_s)
            self.block["shift_day_wait_seconds"] = round(day_wait, 1)
            if day_wait:
                b.sleep(day_wait)
        self._start_log_backend()

        if self.spec is None:
            self._admin = b.es_admin(cfg.es_url, cfg.engineer_key)
        self._clean_armed = True
        self._clean("cleanup_before")
        before_started = b.now()
        before = self._script("before_test")
        self.block["setup"]["before_test"] = asdict(before)
        if not before.ok:
            raise InfraFailure(
                "SETUP_TIMEOUT" if before.timed_out else "SETUP_FAILED",
                f"exit={before.exit_code}",
            )
        self._connect_log_backend()
        if self.db is not None:
            self._provision_db()
        shift_started = b.now()
        self.block["setup"]["ended_at"] = _iso(shift_started)
        try:
            if self.backend == "elasticsearch":
                assert self._admin is not None
                delivered, shift = apply_shift(
                    self._admin, self.qid, prompt, shift_started
                )
            else:
                delivered, shift = shift_question(
                    self.backend,
                    es_admin=self._admin,
                    loki_admin=(
                        b.loki_admin("http://" + self._loki_address())
                        if self.backend == "loki"
                        else None
                    ),
                    qid=self.qid,
                    question=prompt,
                    now=shift_started,
                    setup_started=before_started,
                )
        except ShiftError as error:
            if error.code in INFRA_SHIFT_ERRORS:
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
        assert cfg.k8s is not None
        self.block["disclosures"] = disclosures(
            self.qid, q, shift, k8s_namespace=cfg.k8s.namespace
        )
        if self.todo:
            self.block["disclosures"].append("TODO_UPSTREAM_REMINDER_NOT_COPIED")
        if self.todo_source == "forced":
            self.block["disclosures"].append("TODO_FORCED")
        if self.todo_recorded:
            self.block["todo"] = {
                "mode": cfg.todo_mode,
                "enabled": self.todo,
                "source": self.todo_source,
            }
        self.block["data_plane"] = self._data_plane()

        try:
            pg = b.start_pg(self.dir / "pg", cfg.ports.pg)
        except Exception as error:  # noqa: BLE001 - LabError codes
            raise InfraFailure(getattr(error, "code", "PG_START_FAILED")) from None
        self.cleanup.push("pg", lambda: b.stop_pg(pg))
        identities = self.dir / "target_identities.json"
        write_identities(identities)
        assert cfg.account is not None and cfg.k8s is not None
        addresses = self._backend_addresses() if self.spec is not None else []
        self._owner_dsn = pg.owner_dsn
        db_port = None if self.db is None else self.db.local_port
        wenv = web_env(
            cfg, pg, identities, cfg.account, backend=self.backend,
            knowledge=self.knowledge,
            import_account=cfg.import_account if self.knowledge else None,
            todo=self.todo, db_env=self.db_env or None,
            cluster_name=q.cluster_name,
        )  # fmt: skip
        check_env_isolation(
            wenv, cfg.engineer_key, role="web", backend_addresses=addresses,
            db_port=db_port,
        )  # fmt: skip
        kenv = worker_env(
            cfg, pg, cfg.product_es, cfg.k8s, q.cluster_name, cfg.secrets,
            backend=self.backend, log_env=self.log_env, knowledge=self.knowledge,
            todo=self.todo, db_env=self.db_env or None,
        )  # fmt: skip
        check_env_isolation(
            kenv, cfg.engineer_key, role="worker", backend_addresses=addresses,
            db_port=db_port,
        )  # fmt: skip
        if self.db is not None:
            from opspilot.tracing import check_lab_target

            gate = set(check_lab_target(kenv)) & {"SQL_NOT_LAB", "MONGODB_NOT_LAB"}
            if gate:
                raise RoundAbort("DB_NOT_LAB", ",".join(sorted(gate)))
        try:
            web = self._started(b.start_web(wenv, cfg.ports.web, self.dir / "web.log"))
        except services.ServiceError as error:
            raise InfraFailure(error.code, str(error)) from None
        if self.knowledge:
            # The list is read by the web when the Run is created: import
            # before the worker can pick anything up.
            self._import_knowledge(pg.owner_dsn)
        try:
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
        if self.spec is not None:
            written = (now - before_started).total_seconds()
            self.block["delivery"]["data_written_to_delivery_seconds"] = round(
                written, 3
            )
            limit = DATA_WINDOW_SECONDS.get(self.qid)
            if limit is not None and written > limit:
                raise VoidAttempt("DATA_WINDOW_EXCEEDED", f"{written:.1f}s")
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
        if self.todo or self.todo_source == "forced":
            # PR-E: the Run input offers TodoWrite exactly when this attempt
            # switched it on; otherwise the trial measures the wrong product.
            # (Off by the question: the product default, not read here.)
            offered = task_list.TOOL in b.run_tool_names(pg.owner_dsn, run_id)
            self.block["todo"]["offered"] = offered
            if offered is not self.todo:
                raise DeterministicError(
                    "TODO_SWITCH_MISMATCH", f"enabled={self.todo} offered={offered}"
                )

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
        versions = run_block.get("versions")
        versions = versions if isinstance(versions, Mapping) else {}
        if self.todo_recorded:
            self.block["todo"].update(
                prompt_revision=versions.get("prompt_revision"),
                tool_schema_revision=versions.get("tool_schema_revision"),
            )
        if self.todo:
            self.block["todo"]["stats"] = b.task_list_stats(pg.owner_dsn, run_id)
        if self.knowledge:
            self._check_run_catalog(pg.owner_dsn, run_id)
            self._revoke_knowledge(after_run=True)
            # PR-D order: revoke, stop the web, then ``after_test`` (the
            # stop is idempotent; the cleanup stack's stop is then a no-op).
            web.stop()
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

        for failure in self.forwards.close():
            raise RoundAbort("CLEANUP_FAILED", failure)
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
    cfg: Config,
    qid: str,
    trial: int,
    reason: str,
    history: list[dict[str, Any]],
    question: Question | None = None,
) -> None:
    path = cfg.summary_path(qid, trial)
    block: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "qid": qid,
        "trial": trial,
        "history": history,
        "git": cfg.git,
        "category": "env_broken",
        "time_box_limited": qid in TIME_BOX_LIMITED,
    }
    todo = _todo_block(cfg, qid, question)
    if todo is not None:
        # PR-E review B1: a missing trial of a forced round (or a compared
        # question) records the round's switch too, so resuming checks the
        # same mode and the comparison table puts the row in its cell.
        block["todo"] = todo
    _write_json(
        path,
        {
            "schema": SUMMARY_SCHEMA,
            "status": "missing",
            "reason": reason,
            "frozen_at": datetime.now(UTC).isoformat(),
            "holmes_eval": block,
        },
    )


def _todo_block(
    cfg: Config, qid: str, question: Question | None
) -> dict[str, Any] | None:
    """The ``todo`` block's switch fields, from the config and the question
    alone (no PG); None where summaries carry no block."""
    if question is None:
        if cfg.todo_mode == "question":
            return None
        enabled, source = todo_for(cfg.todo_mode, None)
    else:
        enabled, source = todo_for(cfg.todo_mode, question)
    if not (enabled or source == "forced" or qid in TODO_COMPARED):
        return None
    return {"mode": cfg.todo_mode, "enabled": enabled, "source": source}


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
    _missing(cfg, qid, trial, "CHANCES_EXHAUSTED", history, question)
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
                    question,
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
    """``254,275``, full directory names or the groups ``b1``/``c5``; empty
    is B1. A number naming two questions (``259``) is ``QUESTION_AMBIGUOUS``."""
    if not spec:
        return B1_QUESTIONS
    out: list[str] = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        if item in QUESTION_GROUPS:
            out.extend(QUESTION_GROUPS[item])
            continue
        if item in ALL_QUESTIONS:
            out.append(item)
            continue
        found = [q for q in ALL_QUESTIONS if q.split("_", 1)[0] == item]
        if len(found) > 1:
            raise RoundAbort("QUESTION_AMBIGUOUS", item)
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


def check_todo_mode(cfg: Config) -> None:
    """PR-E: a round keeps one ``--todo`` mode; resuming it with another
    would mix two measures in one round (``TODO_MODE_CHANGED``)."""
    for path in sorted(cfg.evidence_dir.glob("*/trial-*/summary.json")):
        summary = _read_json(path)
        block = summary.get("holmes_eval") if isinstance(summary, Mapping) else None
        todo = block.get("todo") if isinstance(block, Mapping) else None
        if not isinstance(block, Mapping):
            continue
        # A summary without the block ran with each question's own switch.
        mode = todo.get("mode") if isinstance(todo, Mapping) else "question"
        if mode != cfg.todo_mode:
            raise RoundAbort("TODO_MODE_CHANGED", f"{mode} -> {cfg.todo_mode}")


def prepare_round(cfg: Config, backends: Backends, round_: Round) -> None:
    if not _ROUND.match(cfg.round):
        raise RoundAbort("BAD_ROUND", cfg.round)
    check_todo_mode(cfg)
    try:
        verify_upstream(cfg.upstream)
    except UpstreamMismatch as error:
        raise RoundAbort(error.code) from None
    cfg.git = git_state(cfg.allow_dirty)
    private = load_env_file(cfg.env_file)
    for name in (
        *MODEL_KEYS,
        *LANGSMITH_KEYS,
        *JUDGE_KEYS,
        ENGINEER_KEY,
        ADMIN_USER,
        ADMIN_PASSWORD,
    ):
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
    backends_needed = {backend_of(q) for q in cfg.questions}
    if "elasticsearch" in backends_needed:
        _prepare_es(cfg, backends, round_, private)
    _prepare_gateways(cfg, backends, round_, backends_needed)
    cfg.k8s = (
        k8s_from_env_file(cfg.k8s_env_file)
        if cfg.k8s_env_file is not None
        else backends.unreachable_k8s(cfg.round_dir / "k8s-ca", cfg.ports.k8s)
    )
    cfg.account = new_account()
    if any(q in KNOWLEDGE_QUESTIONS for q in cfg.questions):
        cfg.import_account = UiAccount(
            knowledge_mod.IMPORT_ACCOUNT_PREFIX + secrets.token_hex(4),
            secrets.token_urlsafe(24),
        )


def _prepare_gateways(
    cfg: Config, backends: Backends, round_: Round, needed: Collection[str]
) -> None:
    """Rental mode: the round's read-only gateways (C5-U1). The Loki one
    starts pointed at the first Loki question and is switched per attempt;
    the VictoriaLogs one always points at the attempt's VictoriaLogs."""
    plans: list[tuple[str, int, str, str]] = []
    if "loki" in needed and cfg.loki_mode == "gateway":
        first = next(q for q in cfg.questions if backend_of(q) == "loki")
        address = LOG_QUESTIONS[first].address
        assert address is not None
        plans.append(
            ("loki", cfg.ports.loki_gateway, address, "LOKI_GATEWAY_START_FAILED")
        )
    if "victorialogs" in needed and cfg.victorialogs_mode == "docker":
        plans.append(
            (
                "victorialogs",
                cfg.ports.victorialogs_gateway,
                f"127.0.0.1:{cfg.ports.victorialogs}",
                "VICTORIALOGS_GATEWAY_START_FAILED",
            )
        )
    for backend, port, upstream, code in plans:
        if backends.port_in_use(port):
            raise RoundAbort("PORT_BUSY", f"{backend}_gateway")
        try:
            started, env = backends.start_gateway(
                backend,
                port,
                upstream,
                cfg.gateway_state(backend),
                cfg.round_dir / f"{backend}-readonly.env",
            )
        except (services.ServiceError, OSError) as error:
            raise RoundAbort(
                code, getattr(error, "code", type(error).__name__)
            ) from None
        round_.cleanup.push(started.name, started.stop)
        if backend == "loki":
            cfg.product_loki = env
        else:
            cfg.product_victorialogs = env
        if env.get(f"OPSPILOT_{backend.upper()}_URL", "").rstrip(
            "/"
        ) != product_log_url(cfg, backend):
            raise RoundAbort(code, "gateway address differs from the product's")


def _prepare_es(
    cfg: Config, backends: Backends, round_: Round, private: Mapping[str, str]
) -> None:
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
            user = private.get(ADMIN_USER, "")
            password = private.get(ADMIN_PASSWORD, "")
            if not (user and password):
                raise RoundAbort(
                    "ES_ADMIN_CREDENTIALS_MISSING",
                    f"{ADMIN_USER}/{ADMIN_PASSWORD} or --es-readonly-env",
                )
            cfg.product_es = backends.readonly_identity(
                cfg.es_url,
                cfg.engineer_key,
                cfg.round_dir / "es-readonly.env",
                admin=(user, password),
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


#: PR-F: one C6 attempt, in order (``Attempt._run``).
DB_FLOW = (
    "ports_free",
    "after_test",
    "release_check",
    "before_test",
    "port_forward",
    "provision_db_account",
    "db_precheck",
    "no_shift",
    "pg",
    "web",
    "worker",
    "deliver",
    "wait",
    "collect",
    "stop_port_forward",
    "after_test",
    "release_check",
)


def dry_run_plan(cfg: Config, qid: str) -> dict[str, Any]:
    """What a round would do for ``qid``, without touching the host. For a
    C6 question: its spec, the product's env (password masked) and the
    flow."""
    plan: dict[str, Any] = {"qid": qid, "backend": backend_of(qid)}
    db = db_mod.DB_QUESTIONS.get(qid)
    if db is not None:
        env = db_mod.product_env(db, "<random per attempt>")
        plan.update(
            spec=asdict(db),
            product_env=env,
            web_env_keys=[db_mod.url_env_name(db)],
            grants=f"C6-21:{db.dialect}",
            flow=list(DB_FLOW),
        )
    return plan


# -- CLI -----------------------------------------------------------------------------------------------


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--round", required=True)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument(
        "--questions",
        help="comma-separated numbers, names or groups (b1, c5); default B1's 30",
    )
    parser.add_argument("--es", choices=("fake", "external"), default="fake")
    parser.add_argument("--loki", choices=("fake", "gateway"), default="fake")
    parser.add_argument("--victorialogs", choices=("fake", "docker"), default="fake")
    parser.add_argument("--es-url", default="")
    parser.add_argument("--es-readonly-env", type=Path)
    parser.add_argument("--k8s-env-file", type=Path)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument(
        "--env-file", type=Path, help="private KEY=value file (never printed)"
    )
    parser.add_argument("--trace", choices=("off", "lab"), default="off")
    parser.add_argument(
        "--todo",
        choices=TODO_MODES,
        default="question",
        help="TodoWrite: each question's enable_todo, or forced on/off (C1-U2)",
    )
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
        loki_mode=args.loki,
        victorialogs_mode=args.victorialogs,
        todo_mode=args.todo,
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
    many.add_argument(
        "--dry-run",
        action="store_true",
        help="print each question's plan (C6: spec, product env, flow); run nothing",
    )
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
        if args.command == "round" and args.dry_run:
            for qid in cfg.questions:
                print(json.dumps(dry_run_plan(cfg, qid)), flush=True)
            return 0
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
    "check_todo_mode",
    "finish_judging",
    "k8s_from_env_file",
    "load_env_file",
    "prepare_round",
    "resolve_questions",
    "run_question",
    "run_trial",
    "seconds_to_wait_for_utc_midnight",
    "setup_to_delivery_ok",
    "task_list_stats_from_rows",
    "todo_for",
    "tool_env",
    "trial_record",
    "trial_state",
    "unreachable_k8s",
    "web_env",
    "worker_env",
]
