"""Per-question log backends of the C5 questions (#666 PR-C, #569).

Every C5 Loki question brings its own Loki (in its namespace behind a
``kubectl port-forward``, or a docker container on a fixed host port); the two
VictoriaLogs questions write to ``$VICTORIALOGS_URL``. The product never
talks to a backend directly (C5-U1 = A): it holds only the read-only
gateway's account (``scripts/loki_readonly_gateway.py``), whose Loki upstream
is switched to the question's Loki for every attempt; VictoriaLogs gets a
fresh container per attempt (upstream's ``after_test`` never clears it), so it
only ever holds the current question's data (as H-7 for Elasticsearch).

The table below is data from each question's ``port_forwards``,
``before_test`` and ``toolsets.yaml`` (a unit test checks it against the
pinned checkout); nothing here reads an answer.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from scripts.holmes_evals.questions import (
    C2_QUESTIONS,
    C5_LOKI_QUESTIONS,
    C5_VICTORIALOGS_QUESTIONS,
)
from scripts.itbench_lite import services
from scripts.itbench_lite.services import ServiceError, Started

#: ``kubernetes`` (PR-D, C2): no log backend; the product's Kubernetes
#: read-only tools and ``fetch_skill`` only.
Backend = Literal["elasticsearch", "loki", "victorialogs", "kubernetes"]

#: victoriametrics/victoria-logs:v1.54.0, the digest the CI job
#: ``log-backends`` pins (read from Docker Hub on 2026-11-19).
VICTORIALOGS_IMAGE = (
    "victoriametrics/victoria-logs@sha256:"
    "ee4e820cac3721f5f5d91641b950c22439593adc4224f9655e5333d5a84c1dfe"
)
VICTORIALOGS_CONTAINER = "opspilot-eval-victorialogs"
#: After ``after_test``: how long the namespace may take to disappear.
RELEASE_WAIT_S = 180
READY_TIMEOUT_S = 60


@dataclass(frozen=True)
class LogQuestionSpec:
    qid: str
    backend: Literal["loki", "victorialogs", "kubernetes"]
    #: ``127.0.0.1:<port>`` of the question's own Loki (engineer reads and
    #: writes, the gateway's upstream); None for VictoriaLogs (the attempt's).
    address: str | None
    #: The namespace ``after_test`` deletes; it must be gone before the next
    #: attempt.
    namespace: str | None
    #: Host ports the question's setup binds; free before and after.
    host_ports: tuple[int, ...]
    needs_k8s: bool
    #: Upstream's ``toolsets.yaml`` disables the Kubernetes toolsets (R8-15).
    upstream_k8s_disabled: bool


def _loki(qid: str, port: int, namespace: str | None, *, k8s: bool = True,
          extra: tuple[int, ...] = (), k8s_off: bool = False) -> LogQuestionSpec:  # fmt: skip
    return LogQuestionSpec(
        qid, "loki", f"127.0.0.1:{port}", namespace, (port, *extra), k8s, k8s_off
    )


LOG_QUESTIONS: dict[str, LogQuestionSpec] = {
    s.qid: s
    for s in (
        _loki("100a_loki_historical_logs", 3110, "app-100a"),
        _loki("101_loki_historical_logs_pod_deleted", 3101, "app-101"),
        _loki("102_loki_label_discovery", 3102, "app-102"),
        # docker compose publishes Loki on 3100 and Grafana on 3000.
        _loki("102a_loki_logs_transparency", 3100, "app-102a", extra=(3000,)),
        _loki("102b_loki_multiple_pods", 3100, "app-102b", extra=(3000,)),
        _loki(
            "259_loki_historical_logs_pod_deleted_docker",
            3259,
            None,
            k8s=False,
            k8s_off=True,
        ),  # fmt: skip
        _loki("282_loki_custom_stream_label", 10282, "app-282"),
        *(
            LogQuestionSpec(q, "victorialogs", None, None, (), False, True)
            for q in C5_VICTORIALOGS_QUESTIONS
        ),
        # PR-D (C2): namespaces from each question's ``before_test``; 89 and
        # 90 have no setup and need no cluster.
        LogQuestionSpec(
            "89_skill_missing_cloudwatch", "kubernetes", None, None, (), False, False
        ),
        LogQuestionSpec(
            "90_skill_basic_selection", "kubernetes", None, None, (), False, False
        ),
        LogQuestionSpec(
            "94_skill_transparency", "kubernetes", None, "app-94", (), True, False
        ),
        LogQuestionSpec(
            "95_skill_memory_leak_detection",
            "kubernetes",
            None,
            "app-95",
            (),
            True,
            False,
        ),
        LogQuestionSpec(
            "96_no_matching_skill", "kubernetes", None, "app-96", (), True, False
        ),
        LogQuestionSpec(
            "162_get_skills", "kubernetes", None, "app-162", (), True, False
        ),
    )  # fmt: skip
}
assert set(LOG_QUESTIONS) == (
    set(C5_LOKI_QUESTIONS) | set(C5_VICTORIALOGS_QUESTIONS) | set(C2_QUESTIONS)
)
#: Questions whose attempts import their knowledge fixtures and run with
#: ``OPSPILOT_KNOWLEDGE_TOOL=1`` (PR-D).
KNOWLEDGE_QUESTIONS: frozenset[str] = frozenset(C2_QUESTIONS)


def backend_of(qid: str) -> Backend:
    spec = LOG_QUESTIONS.get(qid)
    return "elasticsearch" if spec is None else spec.backend


# -- host-touching defaults (driver ``Backends`` fields) ---------------------------------------


def start_victorialogs(port: int, work_dir: Path) -> Started:
    """A fresh VictoriaLogs (tmpfs storage) on ``127.0.0.1:<port>``."""
    services.remove_container(VICTORIALOGS_CONTAINER)
    services.docker(
        "run", "-d", "--name", VICTORIALOGS_CONTAINER,
        "--cpus=1", "--memory=256m",
        "--tmpfs", "/victoria-logs-data",
        "-p", f"127.0.0.1:{port}:9428",
        VICTORIALOGS_IMAGE, "-storageDataPath=/victoria-logs-data",
    )  # fmt: skip
    started = Started(
        VICTORIALOGS_CONTAINER,
        lambda: services.remove_container(VICTORIALOGS_CONTAINER),
    )
    try:
        services.wait_http(f"http://127.0.0.1:{port}/health", timeout=READY_TIMEOUT_S)
    except BaseException:
        started.stop()
        raise
    started.info = {"image": VICTORIALOGS_IMAGE, "url": f"http://127.0.0.1:{port}"}
    return started


def start_port_forward(
    forward: Mapping[str, Any], env: Mapping[str, str], log_path: Path
) -> Started:
    """Upstream's ``PortForward`` (``kubectl port-forward -n NS svc/S L:R``)
    with the engineer's kubeconfig; ready when the local port answers."""
    local = int(forward["local_port"])
    argv = [
        "kubectl", "port-forward", "-n", str(forward["namespace"]),
        f"svc/{forward['service']}", f"{local}:{int(forward['remote_port'])}",
    ]  # fmt: skip
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
    started.info = {**dict(forward), "pid": proc.pid}
    return started


def namespace_gone(namespace: str, env: Mapping[str, str]) -> bool:
    """True only when kubectl answered and the namespace does not exist
    (``--ignore-not-found``: exit 0 with empty output). Any failure -- no
    kubectl, a timeout, an unreachable API, another error -- is *not*
    released (fail closed, PR #733 review): a namespace that may still hold
    the previous attempt's Loki must never be reused."""
    try:
        done = subprocess.run(
            [
                "kubectl",
                "get",
                "namespace",
                namespace,
                "--ignore-not-found",
                "-o",
                "name",
            ],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and not done.stdout.strip()


def _gateway(argv: list[str]) -> None:
    from scripts import loki_readonly_gateway

    if loki_readonly_gateway.main(argv) != 0:
        raise ServiceError("GATEWAY_FAILED", argv[0])


def start_gateway(
    backend: Literal["loki", "victorialogs"],
    port: int,
    upstream: str,
    state_dir: Path,
    out: Path,
) -> tuple[Started, dict[str, str]]:
    """The read-only gateway (C5-U1) and the product's ``OPSPILOT_*`` values."""
    from scripts.holmes_evals.driver import load_env_file

    _gateway(
        ["start", "--backend", backend, "--upstream", upstream,
         "--state-dir", str(state_dir), "--port", str(port), "--out", str(out)]
    )  # fmt: skip
    started = Started(
        f"{backend}-gateway", lambda: _gateway(["stop", "--backend", backend])
    )
    try:
        services.wait_http(
            f"http://127.0.0.1:{port}/", timeout=READY_TIMEOUT_S, expect=401
        )
    except BaseException:
        started.stop()
        raise
    started.info = {"port": port, "upstream": upstream}
    return started, load_env_file(out)


def switch_gateway(
    backend: Literal["loki", "victorialogs"], upstream: str, state_dir: Path
) -> None:
    _gateway(
        ["switch", "--backend", backend, "--upstream", upstream,
         "--state-dir", str(state_dir)]
    )  # fmt: skip


def start_fake_log_backend(
    backend: Literal["loki", "victorialogs"],
    backend_port: int,
    gateway_port: int,
    work_dir: Path,
) -> tuple[Started, dict[str, str]]:
    """The local stand-in (``fake_logs``): one process, a backend port (no
    authentication, writable) and a gateway port (the product's account,
    the backend's read paths only)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    user, password = "opspilot-investigator", secrets.token_urlsafe(18)
    account = work_dir / f"fake-{backend}-account"
    account.write_text(f"{user}:{password}\n")
    account.chmod(0o600)
    proc = services.spawn(
        [sys.executable, "-m", "scripts.holmes_evals.fake_logs",
         "--backend", backend, "--backend-port", str(backend_port),
         "--gateway-port", str(gateway_port), "--account-file", str(account)],
        services.base_env(),
        work_dir / f"fake-{backend}.log",
        stdout_pipe=True,
    )  # fmt: skip
    started = Started(
        f"fake-{backend}",
        lambda: services.stop_process(proc, 5),
        alive=lambda: proc.poll() is None,
    )
    try:
        info = json.loads(services._read_first_line(proc, 30))
        services.wait_tcp(gateway_port, timeout=30, alive=started.alive)
    except BaseException:
        started.stop()
        raise
    started.info = {**info, "pid": proc.pid}
    prefix = "OPSPILOT_LOKI" if backend == "loki" else "OPSPILOT_VICTORIALOGS"
    return started, {
        f"{prefix}_URL": str(info["gateway_url"]),
        f"{prefix}_USERNAME": user,
        f"{prefix}_PASSWORD": password,
    }


__all__ = [
    "LOG_QUESTIONS",
    "VICTORIALOGS_IMAGE",
    "LogQuestionSpec",
    "backend_of",
    "namespace_gone",
    "start_fake_log_backend",
    "start_gateway",
    "start_port_forward",
    "start_victorialogs",
    "switch_gateway",
]
