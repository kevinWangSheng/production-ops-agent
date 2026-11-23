"""The OpsPilot agent entry SREGym starts for one attempt (#667 PR-B).

``python -m scripts.sregym_eval.agent_entry``, launched by SREGym on the host
(``container_isolation: false``) once the fault is injected. It inherits
SREGym's whole environment -- an admin-backed ``KUBECONFIG``, the judge key,
``API_HOSTNAME=0.0.0.0`` -- and uses none of it: everything comes from the
driver's job file (``SREGYM_EVAL_JOB``). The product's environments are
built from a whitelist and checked (S-5 layer 3).

Flow: job -> wait for the diagnosis stage -> ``/get_app`` -> question ->
port-forwards (``observe``: Prometheus, Jaeger, Loki behind the read-only
gateway) -> PG, web, worker -> manual question on the web -> wait for the
Run (or the deadline) -> report text -> one ``diagnosis`` submission (an
empty string without a report) -> collect the Run -> stop the product ->
wait for ``done`` -> entry record. The record is written on every path.

Exit codes: 0 submitted (possibly empty) and ``done`` seen; 2 bad job;
3 diagnosis stage missed or never reached; 4 product failed to start (an
empty string was still submitted); 5 submission refused; 6 ``aborted``;
7 terminated (SIGTERM at SREGym's agent deadline, or an interrupt) -- the
product is still stopped and the record written. The record is also written
right after the submission and after the Run is collected.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opspilot.tools.redis_inspect import (
    RedisConfig,
    _SocketConnection,
    account_problem,
)
from scripts import run_freeze
from scripts.holmes_evals.driver import load_env_file, report_object
from scripts.holmes_evals.intake import (
    TARGET_ID,
    SubmitResult,
    UiAccount,
    new_account,
    submit_question,
)
from scripts.holmes_evals.log_backends import start_port_forward
from scripts.holmes_evals.text import report_to_text
from scripts.itbench_lite import services
from scripts.itbench_lite.driver import (
    HEALTH_PROFILE_ID,
    Cleanup,
    _pg_start,
    _pg_stop,
    run_state,
)
from scripts.itbench_lite.services import Started
from scripts.sregym_eval.boundary import (
    BoundaryError,
    check_no_problem_id_digest,
    check_product_env,
    problem_id_digest,
)
from scripts.sregym_eval.conductor import AppInfo, ConductorClient, ConductorError
from scripts.sregym_eval.identity import K8sTarget, read_target
from scripts.sregym_eval.job import JOB_ENV, Job, JobError, read_job
from scripts.sregym_eval.question import (
    AnswerLeak,
    QuestionError,
    idempotency_key,
    render_question,
)
from scripts.sregym_eval.summary import Submission

EXIT_OK = 0
EXIT_BAD_JOB = 2
EXIT_STAGE = 3
EXIT_PRODUCT = 4
EXIT_SUBMIT = 5
EXIT_ABORTED = 6
#: SIGTERM (SREGym's agent deadline) or an interrupt.
EXIT_TERMINATED = 7
RECORD = "entry-record.json"
QUESTION_FILE = "question.txt"
RUN_FILE = "run.json"
LEDGER_FILE = "ledger.json"
CLUSTER_UID = "sregym-lite"
OBSERVE_NAMESPACE = "observe"
#: (name, service, remote port, ``Ports`` field) per S-12.
FORWARDS: tuple[tuple[str, str, int, str], ...] = (
    ("prometheus", "prometheus-server", 80, "prometheus"),
    ("jaeger", "jaeger-out", 16686, "jaeger"),
    ("loki", "loki", 3100, "loki"),
)
#: C3 (#567): the app namespace's broker, forwarded for every problem alike
#: (a Kafka-only tool surface would point at the problem).
KAFKA_FORWARD: tuple[str, str, int, str] = ("kafka", "kafka", 9092, "kafka")
#: C3-U1 = a: the benchmark's broker has no authentication.
KAFKA_SECURITY_PROTOCOL = "PLAINTEXT"
#: #809 (B4-U6 = a): SREGym p12's Valkey, forwarded for that problem only.
#: Every pass gets the same Redis tool surface (the Kafka precedent): the
#: address is ``VALKEY_PORT`` everywhere, and only in p12 does anything
#: listen there or does the account exist. (Not a ``Ports`` field: the
#: track's port set is a fixed contract.)
VALKEY_PORT = 29563
VALKEY_PROBLEM = "valkey_auth_disruption"
VALKEY_SEQ = 12
#: (name, service, remote port, local port).
VALKEY_FORWARD: tuple[str, str, int, int] = ("valkey", "valkey-cart", 6379, VALKEY_PORT)
VALKEY_USER = "opspilot_ro"
#: The documented lab grant of #773 (``.github/workflows/ci.yml``); it passes
#: ``redis_inspect.account_problem``. ``>`` + the pass's fresh password is
#: inserted after ``on``.
VALKEY_GRANT: tuple[str, ...] = (
    "~*", "resetchannels", "-@all", "+@read", "+info", "+ping", "+hello",
    "+auth", "+select", "+client|setname", "+config|get", "+slowlog|get",
    "+slowlog|len", "+acl|whoami", "+acl|getuser",
)  # fmt: skip
VALKEY_ACCOUNT_DISCLOSURE = "VALKEY_READONLY_ACCOUNT_BY_HARNESS"
VALKEY_ACCOUNT_FAILED = "VALKEY_READONLY_ACCOUNT_FAILED"
VALKEY_ADMIN_TIMEOUT_S = 10.0
MODEL_KEYS = ("DEEPSEEK_API_KEY",)
LANGSMITH_KEYS = ("LANGSMITH_API_KEY", "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID")
LOKI_ACCOUNT_KEYS = (
    "OPSPILOT_LOKI_USERNAME",
    "OPSPILOT_LOKI_PASSWORD",
    "OPSPILOT_LOKI_TENANT_ID",
)
WORKER_SETTLE_S = 5.0
POLL_S = 5.0


class EntryFailure(RuntimeError):
    def __init__(self, code: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


def valkey_admin(
    argv: Sequence[str], *, port: int, timeout: float = VALKEY_ADMIN_TIMEOUT_S
) -> Any:
    """One command over the p12 port-forward as Valkey's ``default`` user
    (no AUTH is sent; the config's account fields are only there because the
    type requires them). Returns the decoded RESP reply; an error reply
    raises ``RedisReplyError``."""
    config = RedisConfig(
        url=f"redis://127.0.0.1:{port}",
        username="harness-admin",
        password="not-sent",
        tls="disable",
    )
    connection = _SocketConnection(config, timeout)
    try:
        return connection.command(list(argv))
    finally:
        connection.close()


@dataclass
class EntryHost:
    """Everything that touches the host; tests replace any of these."""

    conductor: Callable[[str], ConductorClient] = ConductorClient
    start_port_forward: Callable[..., Started] = start_port_forward
    start_pg: Callable[[Path, int], Any] = _pg_start
    stop_pg: Callable[[Any], None] = _pg_stop
    start_web: Callable[[Mapping[str, str], int, Path], Started] = services.start_web
    start_worker: Callable[..., Started] = services.start_worker
    submit: Callable[..., SubmitResult] = submit_question
    run_state: Callable[[str, str], str | None] = run_state
    collect: Callable[[str, str], tuple[dict[str, Any], dict[str, Any]]] = (
        run_freeze.collect
    )
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    kill: Callable[[int, int], None] = os.kill
    #: #809: one Valkey command as the system-under-test's admin.
    valkey_admin: Callable[..., Any] = valkey_admin


# -- SIGTERM path ----------------------------------------------------------------------------------

#: SREGym SIGKILLs the entry 60 s after its SIGTERM (``main.py:671-711``).
#: All product processes get the signal at once and this long together...
TERM_WAIT_S = 25.0
#: ...and the whole termination (signals, SIGKILL, stop callbacks) this long.
CLEANUP_BUDGET_S = 40.0
KILL_WAIT_S = 3.0


@dataclass(frozen=True)
class ProcTarget:
    name: str
    pid: int
    alive: Callable[[], bool]
    sig: int = signal.SIGTERM


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def postmaster_pid(pg: Any) -> int | None:
    """The postmaster of this attempt's own PG (``postmaster.pid`` line 1,
    checked against the instance's directory and port like ``LocalPgLauncher.stop``)."""
    directory = getattr(pg, "directory", None)
    port = getattr(pg, "port", None)
    if directory is None or port is None:
        return None
    try:
        lines = (Path(directory) / "postmaster.pid").read_text().splitlines()
    except OSError:
        return None
    if len(lines) < 4 or Path(lines[1]) != Path(directory) or lines[3] != str(port):
        return None
    try:
        pid = int(lines[0])
    except ValueError:
        return None
    return pid if pid > 0 else None


def terminate_all(
    targets: list[ProcTarget],
    *,
    wait_s: float = TERM_WAIT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    kill: Callable[[int, int], None] = os.kill,
) -> dict[str, str]:
    """Signal every live target at once, wait ``wait_s`` for all of them
    together, then SIGKILL the rest. Returns ``name -> gone | terminated |
    killed | survived``. Only the registered positive pids are signalled."""
    states: dict[str, str] = {}
    live: list[ProcTarget] = []
    for target in targets:
        if target.pid <= 0 or not target.alive():
            states[target.name] = "gone"
            continue
        try:
            kill(target.pid, target.sig)
            live.append(target)
        except ProcessLookupError:
            states[target.name] = "gone"
    deadline = clock() + wait_s
    while live and clock() < deadline:
        still: list[ProcTarget] = []
        for target in live:
            if target.alive():
                still.append(target)
            else:
                states[target.name] = "terminated"
        live = still
        if live:
            sleep(0.2)
    for target in live:
        if not target.alive():
            states[target.name] = "terminated"
            continue
        try:
            kill(target.pid, signal.SIGKILL)
        except ProcessLookupError:
            states[target.name] = "terminated"
            continue
        states[target.name] = "killed"
    end = clock() + KILL_WAIT_S
    while (
        any(states.get(t.name) == "killed" and t.alive() for t in live)
        and clock() < end
    ):
        sleep(0.1)
    for target in live:
        if states.get(target.name) == "killed" and target.alive():
            states[target.name] = "survived"
    return states


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strs(env: Mapping[str, object]) -> dict[str, str]:
    return {str(k): str(v) for k, v in env.items()}


# -- product environments ------------------------------------------------------------------------


def tool_env(job: Job) -> dict[str, str]:
    p = job.ports
    return {
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "OPSPILOT_OTEL_PROMETHEUS_URL": f"http://127.0.0.1:{p.prometheus}",
        "OPSPILOT_OTEL_JAEGER_URL": f"http://127.0.0.1:{p.jaeger}",
        "OPSPILOT_LOKI_URL": f"http://127.0.0.1:{p.loki_gateway}",
        "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": f"127.0.0.1:{p.kafka}",
        "OPSPILOT_KAFKA_SECURITY_PROTOCOL": KAFKA_SECURITY_PROTOCOL,
        # #809: the address only, the same in web and worker and every pass.
        "OPSPILOT_REDIS_URL": f"redis://127.0.0.1:{VALKEY_PORT}",
        "OPSPILOT_HEALTH_BIND": "",
    }


def missing_to_unreachable(
    env: dict[str, str], job: Job, missing: set[str]
) -> dict[str, str]:
    """A forward that did not start points at the unreachable port (H-11).
    Loki stays on the gateway (its upstream simply does not answer)."""
    dead = f"http://127.0.0.1:{job.ports.unreachable}"
    if "prometheus" in missing:
        env["OPSPILOT_OTEL_PROMETHEUS_URL"] = dead
    if "jaeger" in missing:
        env["OPSPILOT_OTEL_JAEGER_URL"] = dead
    if "kafka" in missing:
        env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] = f"127.0.0.1:{job.ports.unreachable}"
    return env


def web_env(
    job: Job, pg: Any, identities: Path, account: UiAccount, missing: set[str]
) -> dict[str, str]:
    origin = f"http://127.0.0.1:{job.ports.web}"
    env = (
        services.base_env()
        | missing_to_unreachable(tool_env(job), job, missing)
        | {
            "OPSPILOT_WEB_DSN": pg.web_dsn,
            "OPSPILOT_UI_USERS": account.users_env(),
            "OPSPILOT_ALLOWED_ORIGINS": origin,
            "OPSPILOT_AUTH_REVISION": "sregym-lite",
            "OPSPILOT_BIND": f"127.0.0.1:{job.ports.web}",
            "OPSPILOT_SELF_HEAL_INTERVAL_SECONDS": "0",
            "OPSPILOT_TARGET_IDENTITIES": str(identities),
        }
    )
    return _strs(env)


def worker_env(
    job: Job,
    pg: Any,
    target: K8sTarget,
    namespace: str,
    secrets: Mapping[str, str],
    loki_account: Mapping[str, str],
    missing: set[str],
    valkey_account: tuple[str, str],
) -> dict[str, str]:
    env = (
        services.base_env()
        | missing_to_unreachable(tool_env(job), job, missing)
        | {
            "OPSPILOT_WORKER_DSN": pg.runtime_dsn,
            "OPSPILOT_K8S_API_URL": target.api_url,
            "OPSPILOT_K8S_CA_FILE": target.ca_file,
            "OPSPILOT_K8S_TOKEN": target.token,
            "OPSPILOT_K8S_NAMESPACE": namespace,
            "OPSPILOT_TRACE": job.trace,
            "OPSPILOT_WORKER_CAPACITY": "1",
            # #809: the worker's account (real only in p12); plain TCP.
            "OPSPILOT_REDIS_USERNAME": valkey_account[0],
            "OPSPILOT_REDIS_PASSWORD": valkey_account[1],
            "OPSPILOT_REDIS_TLS": "disable",
        }
    )
    env.update({k: loki_account[k] for k in LOKI_ACCOUNT_KEYS if loki_account.get(k)})
    env.update({k: secrets[k] for k in MODEL_KEYS if secrets.get(k)})
    if job.trace == "lab":
        env.update({k: secrets[k] for k in LANGSMITH_KEYS if secrets.get(k)})
        if job.project_name:
            env["LANGSMITH_PROJECT"] = job.project_name
    return _strs(env)


def write_identities(path: Path, namespace: str) -> None:
    """S-11: the extended profile's one target; namespace from ``/get_app``.
    ``workload`` (required by the product loader and migration 0004) repeats
    the namespace: it names the whole application, never a component."""
    entry = {
        "integration_id": TARGET_ID,
        "cluster_uid": CLUSTER_UID,
        "namespace": namespace,
        "workload": namespace,
        "health_profile_id": HEALTH_PROFILE_ID,
        "match": {"version": 1, "labels": {"namespace": namespace}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({TARGET_ID: entry}, indent=1) + "\n")


def forbidden_values(job: Job, environ: Mapping[str, str]) -> list[str]:
    """Values no product process may hold: the engineer kubeconfig (path and
    its secrets), the inherited SREGym kubeconfig path, the judge key, and
    on the proxy path the read-only identity's own token."""
    values = [job.engineer_kubeconfig]
    try:
        text = Path(job.engineer_kubeconfig).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if (
            key in ("token", "client-key-data", "client-certificate-data")
            and value.strip()
        ):
            values.append(value.strip().strip("\"'"))
    for name in ("KUBECONFIG", "JUDGE_API_KEY", "ZAI_API_KEY", "GLM_API_KEY"):
        if environ.get(name):
            values.append(environ[name])
    if job.cluster_path == "filter_proxy_readonly":
        readonly = Path(job.work_dir) / "readonly-kubeconfig.yaml"
        try:
            for line in readonly.read_text(encoding="utf-8").splitlines():
                key, _, value = line.strip().partition(":")
                if key == "token" and value.strip():
                    values.append(value.strip())
        except OSError:
            pass
    return [v for v in values if len(v) >= 8]


# -- the attempt ---------------------------------------------------------------------------------


@dataclass
class Entry:
    job: Job
    host: EntryHost
    environ: Mapping[str, str]
    record: dict[str, Any] = field(default_factory=dict)
    cleanup: Cleanup = field(default_factory=Cleanup)
    client: ConductorClient | None = None
    submitted: bool = False
    procs: list[ProcTarget] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return Path(self.job.work_dir)

    def _write_record(self) -> None:
        path = self.dir / RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(self.record, indent=1, default=str) + "\n")
        os.replace(partial, path)

    def submit(self, text: str) -> None:
        """The one diagnosis submission; only a 200 counts as submitted."""
        assert self.client is not None
        self.submitted = True
        status: int | None = None
        code: str | None = None
        try:
            status = self.client.submit_diagnosis(text)
        except ConductorError as error:
            code = error.code
        self.record["submission"] = asdict(
            Submission(
                submitted=status == 200,
                empty=text == "",
                status_code=status,
                error_code=code,
                text_sha256=_sha256(text) if text else None,
                chars=len(text),
            )
        )
        # Persist at once: SREGym may kill the entry while it judges.
        self._write_record()
        if status != 200:
            raise EntryFailure(code or "SUBMIT_FAILED", EXIT_SUBMIT)

    def run(self) -> int:
        """Every path ends with the record on disk: a SIGTERM (SREGym's
        agent deadline, turned into ``SystemExit`` by :func:`main`) or a
        ``KeyboardInterrupt`` still stops the product and writes it."""
        self.record.update(
            exit_code=None, stage_seen=None, disclosures=[], observability={},
            leak_check=None, submission=None, conversion_reason=None,
            started_at=self.host.now().isoformat(),
        )  # fmt: skip
        exit_code = EXIT_TERMINATED
        try:
            exit_code = self._run_steps()
        except (SystemExit, KeyboardInterrupt) as stop:
            self.record["terminated"] = type(stop).__name__
            exit_code = EXIT_TERMINATED
            self.record["termination"] = self._terminate()
        finally:
            errors = self.cleanup.close()
            if errors:
                self.record.setdefault("cleanup_errors", []).extend(errors)
            if TERMINATED.is_set() and exit_code != EXIT_TERMINATED:
                self.record.setdefault("terminated", "SystemExit")
                exit_code = EXIT_TERMINATED
            self.record["exit_code"] = exit_code
            self.record["ended_at"] = self.host.now().isoformat()
            self._write_record()
        return exit_code

    def _own(self, name: str, started: Started, sig: int = signal.SIGTERM) -> None:
        """Register a started process for the SIGTERM path (its own pid,
        from the handle that started it)."""
        self.cleanup.push(started.name, started.stop)
        pid = started.info.get("pid") if isinstance(started.info, dict) else None
        if isinstance(pid, int) and pid > 0:
            self.procs.append(ProcTarget(name, pid, started.alive, sig))

    def _terminate(self) -> dict[str, Any]:
        """SIGTERM path: signal every product process at once, wait at most
        ``TERM_WAIT_S`` together, SIGKILL what is left, then run the stop
        callbacks (now quick) within what remains of ``CLEANUP_BUDGET_S``."""
        began = self.host.monotonic()
        states = terminate_all(
            self.procs, wait_s=TERM_WAIT_S, clock=self.host.monotonic,
            sleep=self.host.sleep, kill=self.host.kill,
        )  # fmt: skip
        remaining = max(0.0, CLEANUP_BUDGET_S - (self.host.monotonic() - began))
        cleanup, self.cleanup = self.cleanup, Cleanup()
        errors: list[str] = []
        closer = threading.Thread(
            target=lambda: errors.extend(cleanup.close()), daemon=True
        )
        closer.start()
        closer.join(remaining)
        return {
            "processes": states,
            "stop_callbacks": "done" if not closer.is_alive() else "budget_exceeded",
            "errors": errors,
            "seconds": round(self.host.monotonic() - began, 3),
        }

    def _run_steps(self) -> int:
        try:
            try:
                self._run()
            except EntryFailure:
                raise
            except Exception as error:  # noqa: BLE001 - always record and submit
                raise EntryFailure(
                    f"ENTRY_CRASHED:{type(error).__name__}", EXIT_PRODUCT
                ) from None
            exit_code = EXIT_OK
        except EntryFailure as failure:
            self.record["failure"] = failure.code
            exit_code = failure.exit_code
            if failure.exit_code == EXIT_PRODUCT and not self.submitted and self.client:
                try:
                    self.submit("")
                except EntryFailure as late:
                    self.record["failure_after"] = late.code
        # Not in a ``finally``: on SIGTERM ``run`` stops everything at once.
        errors = self.cleanup.close()
        if errors:
            self.record["cleanup_errors"] = errors
        else:
            # Everything registered is stopped: nothing is signalled later.
            self.procs.clear()
        if TERMINATED.is_set():
            # Cleanup swallows a SIGTERM raised inside a stop callback.
            raise SystemExit(128 + signal.SIGTERM)
        if exit_code == EXIT_OK or self.record.get("submission"):
            exit_code = self._wait_done(exit_code)
        return exit_code

    def _wait_done(self, exit_code: int) -> int:
        assert self.client is not None
        try:
            status = self.client.wait_for_done(
                timeout_s=self.job.done_wait_s, poll_s=2.0,
                clock=self.host.monotonic, sleep=self.host.sleep,
            )  # fmt: skip
            self.record["done_seen"] = status.stage == "done"
        except ConductorError as error:
            self.record["done_seen"] = False
            self.record["done_error"] = error.code
            if error.code == "CONDUCTOR_ABORTED":
                return EXIT_ABORTED
        return exit_code

    def _run(self) -> None:
        job, host = self.job, self.host
        self.client = host.conductor(f"http://127.0.0.1:{job.ports.conductor}")
        try:
            status = self.client.wait_for_diagnosis(
                timeout_s=job.diagnosis_wait_s, poll_s=2.0,
                clock=host.monotonic, sleep=host.sleep,
            )  # fmt: skip
            app = self.client.get_app()
        except ConductorError as error:
            raise EntryFailure(
                error.code,
                EXIT_ABORTED if error.code == "CONDUCTOR_ABORTED" else EXIT_STAGE,
            ) from None
        self.record["stage_seen"] = status.stage
        began = host.monotonic()
        remaining = status.remaining_s if status.remaining_s is not None else 1800.0
        deadline = began + max(0.0, remaining - job.submit_margin_s)
        self.record["deadline_s"] = round(deadline - began, 3)
        self.record["app"] = {
            "app_name": app.app_name,
            "namespaces": list(app.namespaces),
        }
        try:
            question = render_question(app)
        except QuestionError as error:
            raise EntryFailure(error.code, EXIT_PRODUCT) from None
        (self.dir / QUESTION_FILE).parent.mkdir(parents=True, exist_ok=True)
        (self.dir / QUESTION_FILE).write_text(question, encoding="utf-8")
        key = idempotency_key(job.round, job.seq, job.trial)
        self.record.update(question_sha256=_sha256(question), idempotency_key=key)
        text = self._product(app, question, key, deadline)
        self.submit(text)

    def _is_valkey_problem(self) -> bool:
        """p12, known from the job: its seq and its problem-id digest."""
        return self.job.seq == VALKEY_SEQ and self.job.problem_digest == (
            problem_id_digest(VALKEY_PROBLEM)
        )

    def _forwards(self, namespace: str) -> set[str]:
        """The three ``observe`` services, then the app namespace's Kafka
        (and, in p12 only, its Valkey)."""
        env = services.base_env() | {"KUBECONFIG": self.job.engineer_kubeconfig}
        missing: set[str] = set()
        plan: list[tuple[str, str, str, int, int]] = [
            (ns, name, service, remote, getattr(self.job.ports, port_field))
            for ns, (name, service, remote, port_field) in [
                *((OBSERVE_NAMESPACE, item) for item in FORWARDS),
                (namespace, KAFKA_FORWARD),
            ]
        ]
        if self._is_valkey_problem():
            plan.append((namespace, *VALKEY_FORWARD))
        for forward_namespace, name, service, remote, local_port in plan:
            forward = {
                "namespace": forward_namespace,
                "service": service,
                "local_port": local_port,
                "remote_port": remote,
            }
            try:
                started = self.host.start_port_forward(
                    forward, env, self.dir / f"port-forward-{name}.log"
                )
            except services.ServiceError:
                missing.add(name)
                self.record["observability"][name] = "missing"
                continue
            self._own(f"port-forward:{name}", started)
            self.record["observability"][name] = "forwarded"
        return missing

    def _valkey_account(self, missing: set[str]) -> tuple[str, str]:
        """#809 (B4-U6 = a): the worker's Redis account. A fresh password per
        pass, held in memory only. In p12 the harness creates the read-only
        user on the system-under-test's Valkey (after ``/get_app``, before
        the product starts) and checks it with the tool's own account check;
        a failure is recorded and the pass goes on. Elsewhere no account
        exists and nothing is sent to any Valkey."""
        password = _secrets.token_hex(24)
        if not self._is_valkey_problem():
            return VALKEY_USER, password
        port = VALKEY_PORT
        code: str | None = None
        if "valkey" in missing:
            code = "FORWARD_MISSING"
        else:
            try:
                reply = self.host.valkey_admin(
                    argv=["ACL", "SETUSER", VALKEY_USER, "reset", "on",
                          f">{password}", *VALKEY_GRANT],
                    port=port,
                )  # fmt: skip
                if reply not in ("OK", b"OK"):
                    code = "SETUSER_NOT_OK"
                else:
                    got = self.host.valkey_admin(
                        argv=["ACL", "GETUSER", VALKEY_USER], port=port
                    )
                    if account_problem(got) is not None:
                        code = "ACCOUNT_NOT_READ_ONLY"
            except Exception as error:  # noqa: BLE001 - recorded, pass goes on
                code = "ADMIN_FAILED:" + type(error).__name__
        self.record["valkey_account"] = {
            "user": VALKEY_USER,
            "status": "created" if code is None else "failed",
            "code": code,
        }
        if code is not None:
            self.record["disclosures"].append(VALKEY_ACCOUNT_FAILED)
        return VALKEY_USER, password

    def _product(self, app: AppInfo, question: str, key: str, deadline: float) -> str:
        job, host = self.job, self.host
        namespace = app.namespaces[0] if app.namespaces else app.namespace
        if len(app.namespaces) > 1:
            self.record["disclosures"].append("K8S_NAMESPACE_FIRST_OF_MANY")
        missing = self._forwards(namespace)
        valkey_account = self._valkey_account(missing)
        try:
            target = read_target(Path(job.k8s_target_file))
        except Exception:  # noqa: BLE001 - IdentityError and file errors
            raise EntryFailure("K8S_TARGET_INVALID", EXIT_PRODUCT) from None
        try:
            secrets = load_env_file(Path(job.secrets_file))
            loki_account = load_env_file(Path(job.loki_env_file))
        except OSError:
            raise EntryFailure("ENTRY_FILES_UNREADABLE", EXIT_PRODUCT) from None
        try:
            pg = host.start_pg(self.dir / "pg", job.ports.pg)
        except (OSError, subprocess.SubprocessError) as error:
            raise EntryFailure(
                f"PG_START_FAILED:{type(error).__name__}", EXIT_PRODUCT
            ) from None
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str):
                raise  # unexpected: ENTRY_CRASHED in run()
            raise EntryFailure(code, EXIT_PRODUCT) from None
        self.cleanup.push("pg", lambda: host.stop_pg(pg))
        postmaster = postmaster_pid(pg)
        if postmaster is not None:
            # SIGINT is PostgreSQL's fast shutdown. Alive only while this
            # instance's own postmaster.pid still names this pid (re-read
            # before every signal), so a reused pid is never signalled.
            def pg_alive(pid: int = postmaster) -> bool:
                return postmaster_pid(pg) == pid and _pid_alive(pid)

            self.procs.append(ProcTarget("pg", postmaster, pg_alive, signal.SIGINT))
        identities = self.dir / "target_identities.json"
        write_identities(identities, namespace)
        account = new_account()
        wenv = web_env(job, pg, identities, account, missing)
        kenv = worker_env(
            job, pg, target, namespace, secrets, loki_account, missing,
            valkey_account,
        )  # fmt: skip
        forbidden = forbidden_values(job, self.environ)
        backend = [f"127.0.0.1:{job.ports.loki}"]
        try:
            check_product_env(
                wenv, role="web", forbidden_values=forbidden, backend_addresses=backend
            )
            check_product_env(
                kenv,
                role="worker",
                forbidden_values=forbidden,
                backend_addresses=backend,
            )
            check_no_problem_id_digest(
                job.problem_digest,
                {"question": question, "idempotency_key": key,
                 "project_name": job.project_name or "",
                 **{f"web:{k}": v for k, v in wenv.items()},
                 **{f"worker:{k}": v for k, v in kenv.items()}},
            )  # fmt: skip
        except (BoundaryError, AnswerLeak) as error:
            self.record["leak_check"] = error.code
            raise EntryFailure(error.code, EXIT_PRODUCT) from None
        self.record["leak_check"] = "ok"
        try:
            self._own("web", host.start_web(wenv, job.ports.web, self.dir / "web.log"))
            worker = host.start_worker(
                kenv, self.dir / "worker.log", settle_seconds=WORKER_SETTLE_S
            )
            self._own("worker", worker)
        except services.ServiceError as error:
            raise EntryFailure(error.code, EXIT_PRODUCT) from None
        try:
            result = host.submit(
                f"http://127.0.0.1:{job.ports.web}", account,
                question=question, key=key, target_id=TARGET_ID,
            )  # fmt: skip
        except OSError as error:
            raise EntryFailure(
                "INTAKE_UNREACHABLE:" + type(error).__name__, EXIT_PRODUCT
            ) from None
        self.record["intake"] = {"status": result.status, "code": result.code}
        if result.status != 201 or not result.run_id:
            # Not retried; the attempt ends with an empty diagnosis (exit 0).
            self.record["conversion_reason"] = "INTAKE_REFUSED"
            return ""
        run_id = result.run_id
        self.record["run_id"] = run_id
        waited = host.monotonic()
        state: str | None = None
        while True:
            state = host.run_state(pg.owner_dsn, run_id)
            if state not in (None, "queued", "running"):
                break
            if not worker.alive() or host.monotonic() >= deadline:
                break
            host.sleep(POLL_S)
        timed_out = state in (None, "queued", "running")
        self.record["run_wait"] = {
            "terminal_state": state,
            "timed_out": timed_out,
            "seconds": round(host.monotonic() - waited, 3),
            "worker_alive": worker.alive(),
        }
        worker.stop()
        run_block, ledger = host.collect(pg.owner_dsn, run_id)
        (self.dir / RUN_FILE).write_text(
            json.dumps(run_block, indent=1, default=str) + "\n"
        )
        (self.dir / LEDGER_FILE).write_text(json.dumps(ledger, default=str) + "\n")
        self._write_record()
        if timed_out:
            # Past the deadline the Run counts as having no report.
            self.record["conversion_reason"] = "RUN_NOT_TERMINAL"
            return ""
        report, reason = report_object(run_block)
        converted = report_to_text(report, None, None)
        self.record["conversion_reason"] = reason if converted.text is None else None
        return converted.text or ""


def main(
    environ: Mapping[str, str] | None = None, host: EntryHost | None = None
) -> int:
    environ = os.environ if environ is None else environ
    path = environ.get(JOB_ENV)
    try:
        if not path:
            raise JobError("JOB_INVALID")
        job = read_job(Path(path))
    except JobError:
        return EXIT_BAD_JOB
    entry = Entry(job, host or EntryHost(), environ)
    TERMINATED.clear()
    previous = _install_sigterm()
    try:
        return entry.run()
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


#: Set by the SIGTERM handler; survives a ``SystemExit`` that a stop
#: callback's error handling swallowed.
TERMINATED = threading.Event()


def _sigterm(signum: int, frame: object) -> None:
    TERMINATED.set()
    raise SystemExit(128 + signum)


def _install_sigterm() -> Any:
    """SIGTERM -> ``SystemExit`` so ``Entry.run``'s cleanup and record run
    (the product processes have their own sessions and outlive a group
    kill). Only in the main thread; returns the previous handler."""
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.signal(signal.SIGTERM, _sigterm)


if __name__ == "__main__":
    sys.exit(main())
