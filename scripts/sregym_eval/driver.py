"""Round driver of the SREGym Lite diagnosis track (#667 PR-B).

Track 「SREGym Lite 诊断段（中立第三方现场题，自测）」. One ``main.py`` per
problem per trial (S-2)::

    results-root lock -> ports free -> context is kind-sregym (S-14)
    -> fresh read-only token -> can-i probe (S-5 layer 2, fails closed)
    -> second filter proxy (S-U2 = c) or direct, product path probe
    -> job + registry -> uv run main.py ... (deploy, inject, start the entry)
    -> stop proxy -> this trial's results CSV -> entry record
    -> problem-id leak recheck -> summary

The Loki read-only gateway (C5-U1 = A) runs for the whole round in front of
the entry's port-forward. Credentials: the engineer's SREGym kubeconfig and
the judge key reach ``main.py`` only; the DeepSeek/LangSmith keys reach the
entry through a mode-600 file; the product holds only the read-only identity
(or the second proxy's token) and the gateway's reader account.

Usage (repository root, on the rental host)::

    .venv/bin/python -m scripts.sregym_eval.driver prepare --upstream <sregym> --kubeconfig <f>
    .venv/bin/python -m scripts.sregym_eval.driver round --round <label> --upstream <sregym> \\
        --kubeconfig <f> --env-file <private KEY=value file> --problems all --trials 1-3
    .venv/bin/python -m scripts.sregym_eval.driver swing ...      # trials 4-6
    .venv/bin/python -m scripts.sregym_eval.driver scores --round <label>

Evidence ``docs/evidence/sregym-lite-<round>/p<NN>/trial-<n>/summary.json``
and ``scores.json``; everything else under ``tmp/sregym-lite/<round>/``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import lab_evidence, run_freeze
from scripts.holmes_evals import scoring
from scripts.holmes_evals.driver import git_state, load_env_file
from scripts.holmes_evals.log_backends import start_gateway
from scripts.itbench_lite import services
from scripts.itbench_lite.driver import DriverError, RoundAbort
from scripts.itbench_lite.services import Started
from scripts.sregym_eval import filter_proxy, identity
from scripts.sregym_eval.agent_entry import (
    KAFKA_SECURITY_PROTOCOL,
    LEDGER_FILE,
    QUESTION_FILE,
    RECORD,
    RUN_FILE,
    VALKEY_ACCOUNT_DISCLOSURE,
    VALKEY_PROBLEM,
)
from scripts.sregym_eval.agent_registry import AGENT_NAME, write_registry
from scripts.sregym_eval.boundary import (
    can_i_verdict,
    check_rbac_manifest,
    problem_id_digest,
)
from scripts.sregym_eval.identity import IdentityError, K8sTarget
from scripts.sregym_eval.job import JOB_ENV, Job, write_job
from scripts.sregym_eval.ports import Ports
from scripts.sregym_eval.question import (
    AnswerLeak,
    check_no_problem_id,
    idempotency_key,
)
from scripts.sregym_eval.results import AttemptOutcome, ResultError, read_attempt
from scripts.sregym_eval.summary import (
    CLUSTER_PATHS,
    Submission,
    scores,
    trial_block,
    trial_record,
)
from scripts.sregym_eval.upstream import (
    LITE_PROBLEMS,
    UpstreamMismatch,
    problem_seq,
    verify_upstream,
)

ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = ROOT / "tmp" / "sregym-lite"
EVIDENCE_ROOT = ROOT / "docs" / "evidence"
EVIDENCE_PREFIX = "sregym-lite-"
SUMMARY_SCHEMA = "sregym-eval-summary-1"
JUDGE_MODEL = "openai/glm-5.3-flashx"
JUDGE_API_BASE = "https://api.z.ai/api/paas/v4"
JUDGE_KEY_NAMES = ("ZAI_API_KEY", "GLM_API_KEY")
MODEL_KEYS = ("DEEPSEEK_API_KEY",)
LANGSMITH_KEYS = ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID")
KIND_CLUSTER_NAME = "sregym"
LOCK_NAME = ".opspilot-sregym.lock"
MAIN_TIMEOUT_S = 3 * 3600.0
MAIN_TRIALS = scoring.MAIN_TRIALS
RERUN_TRIALS = scoring.RERUN_TRIALS
_ROUND = re.compile(r"^[a-z0-9][a-z0-9.-]{0,40}$")
FORBIDDEN_MAIN_FLAGS = (
    "--suite", "--profile", "--noise", "--baseline", "--propagation",
    "--use-external-harness", "--resume", "--agent-timeout",
)  # fmt: skip


# -- host ----------------------------------------------------------------------------------------


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


def _run_main(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    timeout: float,
) -> int:
    """``main.py``'s output (it prints judge results and, on a failed
    judgment, the expected root cause) goes to ``tmp`` only."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        try:
            return subprocess.run(
                list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False,
            ).returncode  # fmt: skip
        except subprocess.TimeoutExpired:
            return -9
        except OSError:
            return 127


def _trace_roots(
    project: str | None, run_id: str, env: Mapping[str, str]
) -> dict[str, Any]:
    return run_freeze.trace_roots(
        project, run_id, client_factory=lambda: lab_evidence.langsmith_client(env)
    )


def port_owner(port: int) -> str:
    """``pid=<n> cmd=<name>`` of the process listening on ``port`` (``lsof``,
    read-only); ``pid=unknown`` when it cannot be told."""
    try:
        done = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=10, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return "pid=unknown"
    pid = cmd = ""
    for line in done.stdout.splitlines():
        if line.startswith("p") and not pid:
            pid = line[1:]
        elif line.startswith("c") and not cmd:
            cmd = line[1:]
    if not pid.isdigit():
        return "pid=unknown"
    return f"pid={pid} cmd={cmd}" if cmd else f"pid={pid}"


@dataclass
class Host:
    """Everything that touches the host; tests replace any of these."""

    kubectl: Callable[..., Any] = _kubectl
    run_main: Callable[..., int] = _run_main
    start_filter_proxy: Callable[..., tuple[Started, K8sTarget]] = (
        filter_proxy.start_filter_proxy
    )
    start_gateway: Callable[..., tuple[Started, dict[str, str]]] = start_gateway
    spawn: Callable[..., Any] = services.spawn
    port_in_use: Callable[[int], bool] = services.port_in_use
    port_owner: Callable[[int], str] = lambda port: port_owner(port)
    http_probe: Callable[..., int] = filter_proxy.http_probe
    trace_roots: Callable[[str | None, str, Mapping[str, str]], dict[str, Any]] = (
        _trace_roots
    )
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep


# -- configuration -------------------------------------------------------------------------------


@dataclass
class Config:
    round: str
    upstream: Path
    kubeconfig: Path
    env_file: Path | None = None
    problems: tuple[str, ...] = LITE_PROBLEMS
    cluster_path: str = "filter_proxy_readonly"
    trace: str = "off"
    ports: Ports = field(default_factory=Ports)
    tmp_root: Path = TMP_ROOT
    evidence_root: Path = EVIDENCE_ROOT
    baseline: Path = identity.DEFAULT_BASELINE
    python: str = sys.executable
    allow_dirty: bool = False
    main_timeout_s: float = MAIN_TIMEOUT_S
    # Filled by prepare_round; never printed or frozen.
    git: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    judge_key: str = field(default="", repr=False)
    loki_env_file: Path | None = None
    #: C3 (#567): the broker's advertised names, which must map to 127.0.0.1
    #: on this host (read only, never written).
    hosts_file: Path = Path("/etc/hosts")
    kafka_hosts: tuple[str, ...] = ("kafka",)

    @property
    def round_dir(self) -> Path:
        return self.tmp_root / self.round

    @property
    def evidence_dir(self) -> Path:
        return self.evidence_root / f"{EVIDENCE_PREFIX}{self.round}"

    @property
    def results_root(self) -> Path:
        return self.upstream / "results"

    @property
    def project_name(self) -> str | None:
        return (
            lab_evidence.lab_project(f"sregym-lite-{self.round}")
            if self.trace == "lab"
            else None
        )

    @property
    def experiment(self) -> str:
        return f"sregym-lite-{self.round}"

    def trial_dir(self, problem_id: str, trial: int) -> Path:
        return self.round_dir / f"p{problem_seq(problem_id):02d}" / f"trial-{trial}"

    def summary_path(self, problem_id: str, trial: int) -> Path:
        seq = problem_seq(problem_id)
        return self.evidence_dir / f"p{seq:02d}" / f"trial-{trial}" / "summary.json"


def main_command(problem_id: str) -> list[str]:
    """S-2, S-9, S-13: one attempt of one problem, diagnosis only."""
    return [
        "uv", "run", "main.py",
        "--problem", problem_id,
        "--n-attempts", "1",
        "--stages", "diagnosis",
        "--agent", AGENT_NAME,
        "--internet-access", "open",
        "--judge-model", JUDGE_MODEL,
    ]  # fmt: skip


def main_env(cfg: Config, judge_key: str, job_path: Path) -> dict[str, str]:
    """``main.py``'s environment: the engineer's kubeconfig, the judge key and
    the job path; no model or LangSmith key."""
    if not judge_key:
        raise RoundAbort("JUDGE_KEY_MISSING")
    return services.base_env() | {
        "KUBECONFIG": str(cfg.kubeconfig),
        "KIND_CLUSTER_NAME": KIND_CLUSTER_NAME,
        "SREGYM_AGENT_REGISTRY": str(cfg.round_dir / "agents.yaml"),
        "API_PORT": str(cfg.ports.conductor),
        "MCP_SERVER_PORT": str(cfg.ports.mcp),
        "K8S_PROXY_PORT": str(cfg.ports.sregym_proxy),
        "JUDGE_API_BASE": JUDGE_API_BASE,
        "JUDGE_API_KEY": judge_key,
        JOB_ENV: str(job_path),
    }


# -- results root ----------------------------------------------------------------------------------


class ResultsLock:
    """``O_EXCL`` lock file in the results root: trials never share it."""

    def __init__(self, results_root: Path) -> None:
        self.path = results_root / LOCK_NAME
        results_root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise RoundAbort("RESULTS_ROOT_LOCKED") from None
        with os.fdopen(fd, "w") as stream:
            stream.write(f"{os.getpid()}\n")

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> ResultsLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def run_dirs(results_root: Path) -> frozenset[Path]:
    if not results_root.is_dir():
        return frozenset()
    return frozenset(p for p in results_root.iterdir() if p.is_dir())


def new_result_csv(
    results_root: Path, problem_id: str, before: frozenset[Path]
) -> Path | None:
    """The CSV under a run directory that did not exist before ``main.py``."""
    name = f"{problem_id}_{AGENT_NAME}_results.csv"
    found = [
        d / AGENT_NAME / problem_id / name
        for d in run_dirs(results_root) - before
        if (d / AGENT_NAME / problem_id / name).is_file()
    ]
    return sorted(found)[-1] if found else None


# -- round -------------------------------------------------------------------------------------------


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    os.replace(partial, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def kafka_hosts_missing(hosts_text: str, names: Sequence[str]) -> list[str]:
    """The ``names`` the host file does not map to 127.0.0.1, in ``names``
    order (hosts syntax: ``#`` comments, address then names, any blanks;
    names compare case-insensitively)."""
    mapped: set[str] = set()
    for line in hosts_text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and fields[0] == "127.0.0.1":
            mapped.update(name.lower() for name in fields[1:])
    return [name for name in names if name.lower() not in mapped]


def check_kafka_hosts(cfg: Config) -> None:
    """``KAFKA_HOSTS_ENTRY_MISSING`` unless every advertised name maps to
    127.0.0.1; an unreadable file counts as all missing."""
    try:
        text = cfg.hosts_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    missing = kafka_hosts_missing(text, cfg.kafka_hosts)
    if missing:
        raise RoundAbort("KAFKA_HOSTS_ENTRY_MISSING", ",".join(missing))


def prepare(cfg: Config, host: Host) -> None:
    """Once, after ``kind/setup_kind_cluster.sh`` and before the first
    ``main.py`` (S-4)."""
    try:
        verify_upstream(cfg.upstream)
        identity.check_context(host, cfg.kubeconfig)
        identity.check_baseline(cfg.baseline)
        check_kafka_hosts(cfg)
        identity.apply_identity(host, cfg.kubeconfig)
    except UpstreamMismatch as error:
        raise RoundAbort(error.code) from None
    except IdentityError as error:
        raise RoundAbort(error.code) from None


def prepare_round(cfg: Config, host: Host) -> list[Started]:
    if not _ROUND.match(cfg.round):
        raise RoundAbort("BAD_ROUND", cfg.round)
    if cfg.cluster_path not in CLUSTER_PATHS:
        raise RoundAbort("CLUSTER_PATH_INVALID", cfg.cluster_path)
    try:
        verify_upstream(cfg.upstream)
    except UpstreamMismatch as error:
        raise RoundAbort(error.code) from None
    cfg.git = git_state(cfg.allow_dirty)
    private = load_env_file(cfg.env_file)
    for name in (*MODEL_KEYS, *LANGSMITH_KEYS, *JUDGE_KEY_NAMES):
        if name not in private and os.environ.get(name):
            private[name] = os.environ[name]
    cfg.secrets = {
        k: v for k, v in private.items() if k in (*MODEL_KEYS, *LANGSMITH_KEYS)
    }
    cfg.judge_key = next((private[k] for k in JUDGE_KEY_NAMES if private.get(k)), "")
    if not cfg.secrets.get("DEEPSEEK_API_KEY"):
        raise RoundAbort("DEEPSEEK_API_KEY_MISSING")
    if not cfg.judge_key:
        raise RoundAbort("JUDGE_KEY_MISSING")
    if cfg.trace == "lab":
        if not cfg.secrets.get("LANGSMITH_API_KEY"):
            raise RoundAbort("LANGSMITH_API_KEY_MISSING")
        project = cfg.project_name
        assert project is not None
        lab_evidence.ensure_longlived_project(
            lab_evidence.langsmith_client(cfg.secrets), project
        )
    try:
        identity.check_context(host, cfg.kubeconfig)
        identity.check_baseline(cfg.baseline)
    except IdentityError as error:
        raise RoundAbort(error.code) from None
    if check_rbac_manifest(identity.RBAC_MANIFEST.read_text(encoding="utf-8")):
        raise RoundAbort("RBAC_MANIFEST_VIOLATION")
    cfg.round_dir.mkdir(parents=True, exist_ok=True)
    write_registry(cfg.round_dir / "agents.yaml", cfg.python, ROOT)
    secrets_file = cfg.round_dir / "entry-secrets.env"
    identity.private_write(
        secrets_file, "".join(f"{k}={v}\n" for k, v in sorted(cfg.secrets.items()))
    )
    check_kafka_hosts(cfg)
    if host.port_in_use(cfg.ports.loki_gateway):
        raise RoundAbort("PORT_BUSY", "loki_gateway")
    cfg.loki_env_file = cfg.round_dir / "loki-readonly.env"
    try:
        gateway, _ = host.start_gateway(
            "loki", cfg.ports.loki_gateway, f"127.0.0.1:{cfg.ports.loki}",
            cfg.round_dir / "loki-gateway", cfg.loki_env_file,
        )  # fmt: skip
    except (services.ServiceError, OSError) as error:
        raise RoundAbort(
            "LOKI_GATEWAY_START_FAILED", getattr(error, "code", "")
        ) from None
    return [gateway]


@dataclass
class TrialOutcome:
    problem_id: str
    trial: int
    status: str
    category: str | None = None
    detail: str = ""


def _product_target(
    cfg: Config, host: Host, work: Path, readonly: K8sTarget
) -> tuple[K8sTarget, Started | None]:
    proxy: Started | None = None
    if cfg.cluster_path == "filter_proxy_readonly":
        proxy, target = host.start_filter_proxy(
            host, cfg.upstream, work / "readonly-kubeconfig.yaml",
            cfg.ports.opspilot_proxy, work,
        )  # fmt: skip
    else:
        target = readonly
    return target, proxy


def run_trial(cfg: Config, host: Host, problem_id: str, trial: int) -> TrialOutcome:
    summary_path = cfg.summary_path(problem_id, trial)
    if summary_path.exists():
        return TrialOutcome(problem_id, trial, "skipped")
    work = cfg.trial_dir(problem_id, trial)
    if work.exists():
        shutil.move(
            str(work), str(work.with_name(f"{work.name}.old-{int(time.time())}"))
        )
    work.mkdir(parents=True)
    busy = [(n, p) for n, p in cfg.ports.per_trial().items() if host.port_in_use(p)]
    if busy:
        # Report only (a previous trial's product process, most likely);
        # nothing is killed here.
        raise RoundAbort(
            "PORT_BUSY",
            ",".join(f"{n}:{p}({host.port_owner(p)})" for n, p in busy),
        )
    timings: dict[str, Any] = {"started_at": host.now().isoformat()}
    boundary: dict[str, Any] = {"rbac": [], "env_isolation": None}
    with ResultsLock(cfg.results_root):
        try:
            identity.check_context(host, cfg.kubeconfig)
            readonly = identity.issue_token(host, cfg.kubeconfig, work)
        except IdentityError as error:
            raise RoundAbort(error.code) from None
        answers = identity.probe_can_i(host, cfg.kubeconfig)
        boundary["can_i"] = answers
        failures = can_i_verdict(identity.answers_by_check(answers))
        if failures:
            raise RoundAbort("CAN_I_FAILED", ",".join(failures))
        proxy: Started | None = None
        try:
            target, proxy = _product_target(cfg, host, work, readonly)
            boundary["product_path"] = filter_proxy.probe_product_path(host, target)
            identity.write_target(work / "product-k8s.json", target)
            assert cfg.loki_env_file is not None
            job = Job(
                round=cfg.round,
                seq=problem_seq(problem_id),
                trial=trial,
                work_dir=str(work),
                cluster_path=cfg.cluster_path,
                k8s_target_file=str(work / "product-k8s.json"),
                engineer_kubeconfig=str(cfg.kubeconfig),
                secrets_file=str(cfg.round_dir / "entry-secrets.env"),
                loki_env_file=str(cfg.loki_env_file),
                problem_digest=problem_id_digest(problem_id),
                ports=cfg.ports,
                trace=cfg.trace,
                project_name=cfg.project_name,
            )
            job_path = work / "job.json"
            write_job(job_path, job)
            before = run_dirs(cfg.results_root)
            timings["main_started_at"] = host.now().isoformat()
            exit_code = host.run_main(
                main_command(problem_id), cfg.upstream,
                main_env(cfg, cfg.judge_key, job_path), work / "main.log",
                cfg.main_timeout_s,
            )  # fmt: skip
            timings["main_ended_at"] = host.now().isoformat()
        except IdentityError as error:
            raise RoundAbort(error.code) from None
        finally:
            if proxy is not None:
                proxy.stop()
        csv_path = new_result_csv(cfg.results_root, problem_id, before)
    return _freeze(
        cfg, host, problem_id, trial, work, csv_path, exit_code, boundary, timings
    )


ENTRY_RECORD_MISSING = "ENTRY_RECORD_MISSING"
#: C3-U1 = a: every trial reads the benchmark's unauthenticated Kafka; only
#: the tool layer keeps it read-only.
KAFKA_NO_AUTH_DISCLOSURE = "KAFKA_NO_AUTH_TOOL_LAYER_ONLY"


def _submission(
    record: Mapping[str, Any], outcome: AttemptOutcome | None = None
) -> Submission:
    """The entry's submission. Without one in the record but with a verdict
    in SREGym's results, SREGym did receive and judge a submission (the entry
    was killed before it could record it): it counts by the CSV verdict, with
    ``error_code = ENTRY_RECORD_MISSING`` and the disclosure of the same name."""
    raw = record.get("submission")
    if not isinstance(raw, Mapping):
        if outcome is not None and outcome.success is not None:
            return Submission(True, False, None, ENTRY_RECORD_MISSING, None, 0)
        return Submission(False, False, None, "NOT_SUBMITTED", None, 0)
    return Submission(
        submitted=raw.get("submitted") is True,
        empty=raw.get("empty") is True,
        status_code=raw.get("status_code")
        if isinstance(raw.get("status_code"), int)
        else None,
        error_code=raw.get("error_code")
        if isinstance(raw.get("error_code"), str)
        else None,
        text_sha256=raw.get("text_sha256")
        if isinstance(raw.get("text_sha256"), str)
        else None,
        chars=int(raw["chars"]) if isinstance(raw.get("chars"), int) else 0,
    )


_RECORD_DROPPED = frozenset({"question_file"})


def recheck_leak(
    problem_id: str,
    *,
    question: str,
    key: str,
    project_name: str | None,
    record: Mapping[str, Any],
) -> None:
    """The driver's recheck with the whole problem id (PR-A rule); a hit
    stops the round before anything reaches the evidence."""
    try:
        check_no_problem_id(
            problem_id,
            {
                "question": question,
                "idempotency_key": key,
                "project_name": project_name or "",
                "entry_record": json.dumps(record, default=str),
            },
        )
    except AnswerLeak as error:
        raise RoundAbort("ANSWER_LEAK", error.code) from None


def _freeze(
    cfg: Config, host: Host, problem_id: str, trial: int, work: Path,
    csv_path: Path | None, exit_code: int, boundary: dict[str, Any],
    timings: dict[str, Any],
) -> TrialOutcome:  # fmt: skip
    raw_record = _read_json(work / RECORD)
    record: dict[str, Any] = dict(raw_record) if isinstance(raw_record, Mapping) else {}
    question_path = work / QUESTION_FILE
    question = (
        question_path.read_text(encoding="utf-8") if question_path.is_file() else ""
    )
    key = idempotency_key(cfg.round, problem_seq(problem_id), trial)
    recheck_leak(problem_id, question=question, key=key,
                 project_name=cfg.project_name, record=record)  # fmt: skip
    boundary["env_isolation"] = record.get("leak_check")
    outcome = None
    if csv_path is not None:
        try:
            outcome = read_attempt(csv_path, 1)
        except ResultError:
            outcome = None
    submission = _submission(record, outcome)
    block = trial_block(
        round=cfg.round,
        problem_id=problem_id,
        trial=trial,
        question=question,
        cluster_path=cfg.cluster_path,  # type: ignore[arg-type]
        submission=submission,
        conversion_reason=record.get("conversion_reason"),
        outcome=outcome,
    )
    block.update(
        entry={k: v for k, v in record.items() if k not in _RECORD_DROPPED},
        boundary=boundary,
        ports=cfg.ports.all(),
        git=cfg.git,
        main_exit_code=exit_code,
        timings=timings,
        data_plane={
            "loki_gateway_port": cfg.ports.loki_gateway,
            "loki_gateway_upstream": f"127.0.0.1:{cfg.ports.loki}",
            "kafka_bootstrap": f"127.0.0.1:{cfg.ports.kafka}",
            "kafka_security_protocol": KAFKA_SECURITY_PROTOCOL,
            "kafka_auth": "none",
            "kafka_advertised_hosts": list(cfg.kafka_hosts),
        },
    )
    block["disclosures"] = list(
        dict.fromkeys(
            [
                *block["disclosures"],
                *record.get("disclosures", []),
                KAFKA_NO_AUTH_DISCLOSURE,
                # #809 (B4-U6 = a): from the problem run, so a lost entry
                # record still carries it.
                *([VALKEY_ACCOUNT_DISCLOSURE] if problem_id == VALKEY_PROBLEM else []),
            ]
        )
    )
    if submission.error_code == ENTRY_RECORD_MISSING:
        block["disclosures"].append(ENTRY_RECORD_MISSING)
        # Whether the lost submission was empty is not known.
        block["submission"]["empty"] = None
    run_id = record.get("run_id")
    run_block = _read_json(work / RUN_FILE)
    ledger = _read_json(work / LEDGER_FILE)
    status = (
        "completed"
        if isinstance(run_id, str) and isinstance(run_block, Mapping)
        else "missing"
    )
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "status": status,
        "run": run_block if status == "completed" else None,
        "trace": host.trace_roots(cfg.project_name, str(run_id), cfg.secrets)
        if status == "completed"
        else None,
        "sregym_eval": block,
    }
    path = cfg.summary_path(problem_id, trial)
    if status == "completed" and isinstance(ledger, Mapping):
        assert isinstance(run_id, str)
        lab_evidence.freeze(
            ledger, experiment=cfg.experiment, run_id=run_id,
            evidence_dir=path.parent, summary=summary,
        )  # fmt: skip
    else:
        summary["frozen_at"] = host.now().isoformat()
        _write_json(path, summary)
    return TrialOutcome(problem_id, trial, status, block["category"])


def round_trials(cfg: Config) -> list[scoring.Trial]:
    out = []
    for path in sorted(cfg.evidence_dir.glob("p*/trial-*/summary.json")):
        summary = _read_json(path)
        if isinstance(summary, Mapping) and isinstance(
            summary.get("sregym_eval"), Mapping
        ):
            out.append(trial_record(summary["sregym_eval"]))
    return out


def set_aside(cfg: Config, problem_id: str, trial: int) -> None:
    path = cfg.summary_path(problem_id, trial)
    if path.exists():
        dest = (
            cfg.round_dir
            / "set-aside"
            / f"p{problem_seq(problem_id):02d}-t{trial}-{int(time.time())}"
        )
        dest.mkdir(parents=True, exist_ok=True)
        for item in path.parent.iterdir():
            shutil.move(str(item), str(dest / item.name))


def run_round(
    cfg: Config, host: Host, trials: Sequence[int], *, swing_only: bool = False
) -> list[TrialOutcome]:
    problems: Sequence[str] = cfg.problems
    if swing_only:
        table = scores([t for t in round_trials(cfg) if t.qid in set(cfg.problems)])
        problems = [p for p in cfg.problems if p in set(table["swing_questions"])]
    started = prepare_round(cfg, host)
    outcomes: list[TrialOutcome] = []
    try:
        for problem_id in problems:
            for trial in trials:
                result = run_trial(cfg, host, problem_id, trial)
                outcomes.append(result)
                print(json.dumps({"seq": problem_seq(problem_id), "trial": trial,
                                  "status": result.status, "category": result.category}),
                      flush=True)  # fmt: skip
    finally:
        for item in reversed(started):
            item.stop()
    return outcomes


# -- CLI -------------------------------------------------------------------------------------------


def resolve_problems(spec: str | None) -> tuple[str, ...]:
    """``all`` (default), or comma-separated 1-based positions or ids."""
    if not spec or spec == "all":
        return LITE_PROBLEMS
    out: list[str] = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        if item.isdigit() and 1 <= int(item) <= len(LITE_PROBLEMS):
            out.append(LITE_PROBLEMS[int(item) - 1])
        elif item in LITE_PROBLEMS:
            out.append(item)
        else:
            raise RoundAbort("PROBLEM_UNKNOWN", item)
    return tuple(dict.fromkeys(out))


def resolve_trials(spec: str, allowed: Sequence[int]) -> tuple[int, ...]:
    lo, _, hi = spec.partition("-")
    try:
        first, last = int(lo), int(hi or lo)
    except ValueError:
        raise RoundAbort("TRIALS_INVALID", spec) from None
    picked = tuple(range(first, last + 1))
    if not picked or any(t not in allowed for t in picked):
        raise RoundAbort("TRIALS_INVALID", spec)
    return picked


def _redo(spec: str) -> tuple[str, int]:
    match = re.match(r"^p(\d{1,2}):t(\d)$", spec)
    if not match or not 1 <= int(match.group(1)) <= len(LITE_PROBLEMS):
        raise RoundAbort("REDO_INVALID", spec)
    return LITE_PROBLEMS[int(match.group(1)) - 1], int(match.group(2))


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--round", required=True)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument(
        "--env-file", type=Path, help="private KEY=value file (never printed)"
    )
    parser.add_argument("--problems", default="all")
    parser.add_argument(
        "--cluster-path", choices=CLUSTER_PATHS, default="filter_proxy_readonly"
    )
    parser.add_argument("--trace", choices=("off", "lab"), default="off")
    parser.add_argument(
        "--redo", action="append", default=[], help="pNN:tN, set aside and rerun"
    )
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--tmp-root", type=Path, default=TMP_ROOT)
    parser.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    parser.add_argument("--baseline", type=Path, default=identity.DEFAULT_BASELINE)


def _config(args: argparse.Namespace) -> Config:
    return Config(
        round=args.round,
        upstream=args.upstream.resolve(),
        kubeconfig=args.kubeconfig.resolve(),
        env_file=args.env_file,
        problems=resolve_problems(args.problems),
        cluster_path=args.cluster_path,
        trace=args.trace,
        tmp_root=args.tmp_root.resolve(),
        evidence_root=args.evidence_root.resolve(),
        baseline=args.baseline,
        allow_dirty=args.allow_dirty,
    )


def main(argv: Sequence[str] | None = None, host: Host | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="create the read-only identity (S-4)")
    prep.add_argument("--upstream", required=True, type=Path)
    prep.add_argument("--kubeconfig", required=True, type=Path)
    prep.add_argument("--baseline", type=Path, default=identity.DEFAULT_BASELINE)
    many = sub.add_parser("round", help="each problem, trials 1..3")
    _common(many)
    many.add_argument("--trials", default="1-3")
    swing = sub.add_parser("swing", help="trials 4-6 of the swinging problems")
    _common(swing)
    swing.add_argument("--trials", default="4-6")
    table = sub.add_parser("scores", help="scores.json of a round")
    table.add_argument("--round", required=True)
    table.add_argument("--out", type=Path)
    table.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    args = parser.parse_args(argv)
    host = host or Host()
    try:
        if args.command == "prepare":
            cfg = Config(round="prepare", upstream=args.upstream.resolve(),
                         kubeconfig=args.kubeconfig.resolve(), baseline=args.baseline)  # fmt: skip
            prepare(cfg, host)
            print(json.dumps({"status": "prepared"}))
            return 0
        if args.command == "scores":
            cfg = Config(round=args.round, upstream=Path(), kubeconfig=Path(),
                         evidence_root=args.evidence_root.resolve())  # fmt: skip
            result = scores(round_trials(cfg))
            out = args.out or cfg.evidence_dir / "scores.json"
            _write_json(out, result)
            print(json.dumps({"out": str(out), "main": result["main"]}))
            return 0
        cfg = _config(args)
        allowed = MAIN_TRIALS if args.command == "round" else RERUN_TRIALS
        trials = resolve_trials(args.trials, allowed)
        for spec in args.redo:
            set_aside(cfg, *_redo(spec))
        run_round(cfg, host, trials, swing_only=args.command == "swing")
        return 0
    except DriverError as error:
        print(
            json.dumps(
                {"status": "aborted", "code": error.code, "detail": error.detail}
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
