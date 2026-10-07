"""The independent Observer process: ``python -m opspilot.observer``.

Deterministic recovery observation (C3 section 10, F6) in its own process
with its own credentials (C3 section 3, decision D3): a PostgreSQL login that
is a member of ``opspilot_observer`` (migration 0003 grants it exactly the
sampling path) and a read-only Prometheus endpoint issued to the Observer.
It never calls a model and never imports the investigation worker, the tool
gateway or their configuration; the variables below are the only ones read,
and the investigation side's ``OPSPILOT_DSN`` / ``OPSPILOT_OTEL_*`` /
``DEEPSEEK_API_KEY`` are neither read nor used as fallbacks.

Configuration (environment only):

* ``OPSPILOT_OBSERVER_DSN``               PostgreSQL DSN of the Observer login (required).
* ``OPSPILOT_OBSERVER_PROMETHEUS_URL``    the Observer's Prometheus base URL (required).
* ``OPSPILOT_OBSERVER_PROMETHEUS_TOKEN``  bearer token, optional; or
* ``OPSPILOT_OBSERVER_ENV_FILE``          a private ``KEY=value`` file holding it.
* ``OPSPILOT_OBSERVER_POLL_SECONDS``      idle poll interval (default 5).
* ``OPSPILOT_OBSERVER_BATCH``             leases per poll (default 20).
* ``OPSPILOT_OBSERVER_GRACE_SECONDS``     SIGTERM join bound (default 30).

Like the worker it refuses to start unless the schema is at the Alembic head
(the role needs ``SELECT`` on ``alembic_version``, which 0003 grants).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opspilot.observation.store import OBSERVER_LEASE_SECONDS, ObservationStore
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import PrometheusReadOnlySource

__all__ = ["build_loop", "main"]

_log = logging.getLogger("opspilot.observer")

DSN_VAR = "OPSPILOT_OBSERVER_DSN"
PROMETHEUS_URL_VAR = "OPSPILOT_OBSERVER_PROMETHEUS_URL"
PROMETHEUS_TOKEN_VAR = "OPSPILOT_OBSERVER_PROMETHEUS_TOKEN"
ENV_FILE_VAR = "OPSPILOT_OBSERVER_ENV_FILE"


def _read_env_file(path: Path, name: str) -> str:
    """``name``'s value from a private ``KEY=value`` file; never echoed."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def _token(env: Mapping[str, str]) -> str | None:
    token = env.get(PROMETHEUS_TOKEN_VAR, "")
    if not token and env.get(ENV_FILE_VAR):
        token = _read_env_file(
            Path(env[ENV_FILE_VAR]).expanduser(), PROMETHEUS_TOKEN_VAR
        )
    return token or None


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number") from None
    if not value > 0:
        raise SystemExit(f"{name} must be positive")
    return value


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None
    if value < 1:
        raise SystemExit(f"{name} must be positive")
    return value


def build_loop(
    env: Mapping[str, str],
    *,
    stop: threading.Event,
    store: ObservationStore | None = None,
) -> ObserverLoop:
    """Compose the Observer from its own variables only."""
    dsn = env.get(DSN_VAR)
    if not dsn:
        raise SystemExit(f"{DSN_VAR} is required")
    url = env.get(PROMETHEUS_URL_VAR)
    if not url:
        raise SystemExit(f"{PROMETHEUS_URL_VAR} is required")
    try:
        source = PrometheusReadOnlySource(url, token=_token(env))
    except ValueError:
        raise SystemExit(f"{PROMETHEUS_URL_VAR} must be an http(s) URL") from None
    store = store or ObservationStore(dsn)
    loop = ObserverLoop(
        store=store,
        source=source,
        stop=stop,
        poll_seconds=_float_env(env, "OPSPILOT_OBSERVER_POLL_SECONDS", 5.0),
        batch=_int_env(env, "OPSPILOT_OBSERVER_BATCH", 20),
    )
    _log.info("observer owner=%s prometheus=%s", loop.owner, source.endpoint)
    return loop


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    env = os.environ
    grace = _float_env(env, "OPSPILOT_OBSERVER_GRACE_SECONDS", 30.0)
    stop = threading.Event()
    loop = build_loop(env, stop=stop)
    loop.store.install()

    def request_stop(signum: int, _frame: Any) -> None:
        _log.info("signal=%s: stop claiming, finishing the in-flight sample", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    thread = threading.Thread(target=loop.run, name="opspilot-observer", daemon=True)
    thread.start()
    _log.info("observer started")
    while not stop.wait(0.5):
        if not thread.is_alive():
            _log.error("observer loop died; exiting")
            return 1
    thread.join(grace)
    if thread.is_alive():
        _log.warning(
            "sample still in flight after grace=%ss: exiting; its lease lapses "
            "within %ss and the next claim retries the same sequence",
            grace,
            OBSERVER_LEASE_SECONDS,
        )
        return 1
    loop.store.close()
    _log.info("observer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
