"""Real launchers for the ITBench-Lite driver (#604, L-D3/L-D4/L-D5/L-D20).

Everything here starts something on this host: docker containers for the
replay Prometheus (with ``promtool`` from the same image) and the replay
Jaeger, and subprocesses for the fake Kubernetes API server, the web and the
worker. All of it binds or publishes ``127.0.0.1`` only. The driver reaches
these through ``driver.Backends`` so its orchestration is testable with
fakes; nothing in this module is exercised by the unit tests except the
process-stop helper.

Stopping follows the user's global rule: only processes this driver started,
through their ``Popen`` handle (SIGTERM, grace, then SIGKILL of the same PID),
and containers by their full name (``docker rm -f <name>``).
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

ROOT = Path(__file__).resolve().parents[2]
PROMETHEUS_IMAGE = "prom/prometheus:v3.5.0"
JAEGER_IMAGE = "jaegertracing/all-in-one:1.53.0"
#: The lab's 5000 counts trace ids; one replayed scenario holds far more.
JAEGER_MAX_TRACES = 1_000_000
#: Variables any child inherits (no model, trace or lab credential).
BASE_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "TZ")
DOCKER_TIMEOUT_S = 120
PROMTOOL_TIMEOUT_S = 1800
READY_TIMEOUT_S = 120
FAKE_K8S_LOAD_TIMEOUT_S = 900
PROCESS_GRACE_S = 45.0


class ServiceError(RuntimeError):
    """A replay service or host process did not start or stop as required."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class Started:
    """One started resource; ``stop`` is idempotent."""

    name: str
    _stop: Callable[[], None]
    info: dict[str, Any] = field(default_factory=dict)
    alive: Callable[[], bool] = lambda: True
    _stopped: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._stop()


def base_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if k in BASE_ENV_KEYS or k.startswith("LC_")}


# -- processes ---------------------------------------------------------------


def stop_process(proc: subprocess.Popen[Any], grace: float = PROCESS_GRACE_S) -> None:
    """SIGTERM through the handle, wait ``grace``, then SIGKILL the same PID."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        raise ServiceError("PROCESS_STOP_FAILED", str(proc.pid)) from None


def spawn(
    argv: Sequence[str],
    env: Mapping[str, str],
    log_path: Path,
    *,
    stdout_pipe: bool = False,
) -> subprocess.Popen[Any]:
    """Start a child in its own session (a terminal Ctrl-C reaches only the
    driver), cwd the repository root, output to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log: IO[bytes] = log_path.open("ab")
    try:
        return subprocess.Popen(
            list(argv),
            cwd=ROOT,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if stdout_pipe else log,
            stderr=log,
            start_new_session=True,
        )
    finally:
        log.close()


def wait_tcp(port: int, *, timeout: float, alive: Callable[[], bool]) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive():
            raise ServiceError("PROCESS_EXITED", f"port {port}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.5)
    raise ServiceError("NOT_READY", f"port {port}")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_status(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    context: ssl.SSLContext | None = None,
    timeout: float = 5,
) -> int | None:
    """Status of one GET to a loopback URL; ``None`` when nothing answered."""
    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        if context is not None:
            handler = urllib.request.HTTPSHandler(context=context)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), handler
            )
        else:
            opener = _OPENER
        with opener.open(request, timeout=timeout) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError):
        return None


def wait_http(
    url: str,
    *,
    timeout: float,
    expect: int = 200,
    alive: Callable[[], bool] = lambda: True,
    **kwargs: Any,
) -> None:
    deadline = time.monotonic() + timeout
    last: int | None = None
    while time.monotonic() < deadline:
        if not alive():
            raise ServiceError("PROCESS_EXITED", url)
        last = http_status(url, **kwargs)
        if last == expect:
            return
        time.sleep(1)
    raise ServiceError("NOT_READY", f"{url} last={last}")


# -- docker ------------------------------------------------------------------


def docker(*args: str, timeout: float = DOCKER_TIMEOUT_S) -> str:
    try:
        done = subprocess.run(
            ["docker", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=base_env() | _docker_env(),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        stderr = getattr(e, "stderr", "") or ""
        raise ServiceError("DOCKER_FAILED", f"{args[0]}: {stderr[-300:]}") from None
    return done.stdout


def _docker_env() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items() if k in ("DOCKER_HOST", "DOCKER_CONFIG")
    }


def remove_container(name: str) -> None:
    """``docker rm -f`` by full name; absent is fine."""
    try:
        docker("rm", "-f", name)
    except ServiceError:
        listed = docker(
            "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"
        )
        if listed.strip():
            raise


def image_digest(image: str) -> str | None:
    try:
        out = docker("image", "inspect", "--format", "{{json .RepoDigests}}", image)
        digests = json.loads(out)
        return digests[0] if digests else None
    except (ServiceError, ValueError, IndexError):
        return None


def _user() -> str:
    return f"{os.getuid()}:{os.getgid()}"


PROMETHEUS_CONFIG = "global:\n  evaluation_interval: 1m\n"


def start_prometheus(
    name: str, openmetrics: Path, work_dir: Path, port: int
) -> Started:
    """``promtool tsdb create-blocks-from openmetrics`` then a Prometheus that
    serves only those blocks (no scrape jobs, no rules) on 127.0.0.1:port."""
    tsdb = work_dir / "tsdb"
    tsdb.mkdir(parents=True, exist_ok=True)
    config = work_dir / "prometheus.yml"
    config.write_text(PROMETHEUS_CONFIG)
    docker(
        "run", "--rm", "--user", _user(), "--network", "none",
        "--entrypoint", "promtool",
        "-v", f"{openmetrics.resolve()}:/in/metrics.om:ro",
        "-v", f"{tsdb.resolve()}:/tsdb",
        PROMETHEUS_IMAGE,
        "tsdb", "create-blocks-from", "openmetrics", "/in/metrics.om", "/tsdb",
        timeout=PROMTOOL_TIMEOUT_S,
    )  # fmt: skip
    docker(
        "run", "-d", "--name", name, "--user", _user(),
        "-p", f"127.0.0.1:{port}:9090",
        "-v", f"{tsdb.resolve()}:/prometheus",
        "-v", f"{config.resolve()}:/etc/prometheus/prometheus.yml:ro",
        PROMETHEUS_IMAGE,
        "--config.file=/etc/prometheus/prometheus.yml",
        "--storage.tsdb.path=/prometheus",
        "--storage.tsdb.retention.time=30d",
    )  # fmt: skip
    started = Started(name, lambda: remove_container(name))
    try:
        wait_http(f"http://127.0.0.1:{port}/-/ready", timeout=READY_TIMEOUT_S)
    except BaseException:
        started.stop()
        raise
    started.info = {"image": PROMETHEUS_IMAGE, "digest": image_digest(PROMETHEUS_IMAGE)}
    return started


def start_jaeger(name: str, query_port: int, otlp_port: int) -> Started:
    """Jaeger all-in-one 1.53 (the lab's version) with only the query and
    OTLP/HTTP ports published on 127.0.0.1; in-memory store."""
    docker(
        "run", "-d", "--name", name,
        "-p", f"127.0.0.1:{query_port}:16686",
        "-p", f"127.0.0.1:{otlp_port}:4318",
        "-e", "COLLECTOR_OTLP_ENABLED=true",
        "-e", "SPAN_STORAGE_TYPE=memory",
        "-e", f"MEMORY_MAX_TRACES={JAEGER_MAX_TRACES}",
        JAEGER_IMAGE,
    )  # fmt: skip
    started = Started(name, lambda: remove_container(name))
    try:
        wait_http(
            f"http://127.0.0.1:{query_port}/api/services", timeout=READY_TIMEOUT_S
        )
    except BaseException:
        started.stop()
        raise
    started.info = {"image": JAEGER_IMAGE, "digest": image_digest(JAEGER_IMAGE)}
    return started


# -- host processes ----------------------------------------------------------


def _read_first_line(proc: subprocess.Popen[Any], timeout: float) -> str:
    box: list[bytes] = []
    reader = threading.Thread(
        target=lambda: box.append(proc.stdout.readline() if proc.stdout else b""),
        daemon=True,
    )
    reader.start()
    reader.join(timeout)
    if not box or not box[0]:
        raise ServiceError("FAKE_K8S_NOT_READY", "no start line")
    return box[0].decode()


def start_fake_k8s(
    snapshot: Path,
    *,
    offset_seconds: int,
    token: str,
    work_dir: Path,
    port: int,
    load_timeout: float = FAKE_K8S_LOAD_TIMEOUT_S,
) -> Started:
    """The #583 server as a subprocess; ready once a token GET answers 200."""
    work_dir.mkdir(parents=True, exist_ok=True)
    token_file = work_dir / "token"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(token + "\n")
    proc = spawn(
        [
            sys.executable, "-m", "scripts.itbench_lite.fake_k8s", str(snapshot),
            "--offset-seconds", str(offset_seconds),
            "--token-file", str(token_file),
            "--cert-dir", str(work_dir / "cert"),
            "--host", "127.0.0.1", "--port", str(port),
        ],
        base_env(),
        work_dir / "fake_k8s.log",
        stdout_pipe=True,
    )  # fmt: skip
    started = Started(
        "fake_k8s", lambda: stop_process(proc, 10), alive=lambda: proc.poll() is None
    )
    try:
        line = json.loads(_read_first_line(proc, load_timeout))
        api_url, ca_file = str(line["api_url"]), str(line["ca_file"])
        context = ssl.create_default_context(cafile=ca_file)
        wait_http(
            f"{api_url}/api/v1/pods?limit=1",
            timeout=READY_TIMEOUT_S,
            alive=started.alive,
            headers={"Authorization": f"Bearer {token}"},
            context=context,
        )
    except BaseException:
        started.stop()
        raise
    started.info = {"api_url": api_url, "ca_file": ca_file, "pid": proc.pid}
    return started


def start_web(env: Mapping[str, str], port: int, log_path: Path) -> Started:
    proc = spawn([sys.executable, "-m", "opspilot.web", "serve"], env, log_path)
    started = Started(
        "web", lambda: stop_process(proc, 15), alive=lambda: proc.poll() is None
    )
    try:
        wait_tcp(port, timeout=READY_TIMEOUT_S, alive=started.alive)
    except BaseException:
        started.stop()
        raise
    started.info = {"pid": proc.pid}
    return started


def start_worker(
    env: Mapping[str, str], log_path: Path, *, settle_seconds: float = 5.0
) -> Started:
    """The worker has no readiness signal with its health listener off
    (L-D3): it counts as started when still alive after ``settle_seconds``."""
    proc = spawn([sys.executable, "-m", "opspilot.worker_main"], env, log_path)
    started = Started(
        "worker", lambda: stop_process(proc), alive=lambda: proc.poll() is None
    )
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            started.stop()
            raise ServiceError("WORKER_EXITED", f"rc={proc.returncode}")
        time.sleep(0.2)
    started.info = {"pid": proc.pid}
    return started


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def memory_reading() -> dict[str, Any] | None:
    """``free -m`` on Linux (L-D17); ``None`` where it does not exist."""
    try:
        out = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = {}
    lines = out.splitlines()
    header = lines[0].split() if lines else []
    for line in lines[1:]:
        name, *values = line.split()
        rows[name.rstrip(":").lower()] = dict(
            zip(header, map(int, values), strict=False)
        )
    return {"unit": "MiB", **rows}
