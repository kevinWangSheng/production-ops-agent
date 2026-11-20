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
empty string was still submitted); 5 submission refused; 6 ``aborted``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    """S-11: the extended profile's one target; namespace from ``/get_app``,
    no workload (it would point the model at a component)."""
    entry = {
        "integration_id": TARGET_ID,
        "cluster_uid": CLUSTER_UID,
        "namespace": namespace,
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
        if status != 200:
            raise EntryFailure(code or "SUBMIT_FAILED", EXIT_SUBMIT)

    def run(self) -> int:
        self.record.update(
            exit_code=None, stage_seen=None, disclosures=[], observability={},
            leak_check=None, submission=None, conversion_reason=None,
            started_at=self.host.now().isoformat(),
        )  # fmt: skip
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
        finally:
            errors = self.cleanup.close()
            if errors:
                self.record["cleanup_errors"] = errors
        if exit_code == EXIT_OK or self.record.get("submission"):
            exit_code = self._wait_done(exit_code)
        self.record["exit_code"] = exit_code
        self.record["ended_at"] = self.host.now().isoformat()
        self._write_record()
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

    def _forwards(self) -> set[str]:
        env = services.base_env() | {"KUBECONFIG": self.job.engineer_kubeconfig}
        missing: set[str] = set()
        for name, service, remote, port_field in FORWARDS:
            forward = {
                "namespace": OBSERVE_NAMESPACE,
                "service": service,
                "local_port": getattr(self.job.ports, port_field),
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
            self.cleanup.push(started.name, started.stop)
            self.record["observability"][name] = "forwarded"
        return missing

    def _product(self, app: AppInfo, question: str, key: str, deadline: float) -> str:
        job, host = self.job, self.host
        namespace = app.namespaces[0] if app.namespaces else app.namespace
        if len(app.namespaces) > 1:
            self.record["disclosures"].append("K8S_NAMESPACE_FIRST_OF_MANY")
        missing = self._forwards()
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
        identities = self.dir / "target_identities.json"
        write_identities(identities, namespace)
        account = new_account()
        wenv = web_env(job, pg, identities, account, missing)
        kenv = worker_env(job, pg, target, namespace, secrets, loki_account, missing)
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
            self.cleanup.push(
                "web", host.start_web(wenv, job.ports.web, self.dir / "web.log").stop
            )
            worker = host.start_worker(
                kenv, self.dir / "worker.log", settle_seconds=WORKER_SETTLE_S
            )
            self.cleanup.push("worker", worker.stop)
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
    return Entry(job, host or EntryHost(), environ).run()


if __name__ == "__main__":
    sys.exit(main())
