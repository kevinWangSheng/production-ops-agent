"""Development entry point: ``python -m opspilot.web serve`` behind a trusted proxy.

Configuration comes from the environment only, and only as hashes:

* ``OPSPILOT_WEB_DSN``        PostgreSQL DSN of the business-state authority, as
  a login role in ``opspilot_web`` (migration 0011); never the migration
  owner's ``OPSPILOT_DSN``, which this process does not read.
* ``OPSPILOT_UI_USERS``       ``name=<pbkdf2 hash>`` entries separated by ``,``.
* ``OPSPILOT_EVENT_TOKENS``   ``<sha256 hex>=<actor id>`` entries separated by ``,``.
* ``OPSPILOT_AUTH_REVISION``  identifier recorded on every principal.
* ``OPSPILOT_ALLOWED_ORIGINS`` origins accepted for browser form posts.
* ``OPSPILOT_BIND``           ``host:port``; defaults to ``127.0.0.1:8080``.
* ``OPSPILOT_RUN_SECONDS``    Run wall for new Runs (default: the frozen
  ``RUN_WALL_SECONDS``); a dev knob to exercise the deadline sweep, never
  above the freeze.
* ``OPSPILOT_TOOL_PROFILE``   ``fixture`` (default) or ``otel-demo``; must
  match the worker (``opspilot.tools.profiles``).
* ``OPSPILOT_TARGET_IDENTITIES`` JSON file mapping an operator
  ``target_id`` to its immutable identity (``opspilot.schema
  .load_target_identities``); read when a remediation is registered and
  to bind an Alertmanager alert by an entry's ``match`` labels; optional
  (without it registrations are refused for lack of identity and every
  alert is handed to a human without a Run).
* ``OPSPILOT_HEALTH_PROFILE``  path of the HealthProfile JSON the
  "register remediation" action fixes an observation session by (default:
  the shipped ``otel-demo-checkout`` profile).
* ``OPSPILOT_SELF_HEAL_INTERVAL_SECONDS`` seconds between two automatic
  observation passes (``Workbench.self_heal_pass``, #184): a background
  thread authorizes the recovery observation of incidents whose conclusion
  was published and on which no remediation was recorded, under this
  process's ``opspilot_web`` role. Default ``30`` (on; the F6/F13
  projections count the automatic authorization as the system's, not a
  human action, #184 step 2); ``0`` turns it off, any other positive value
  sets the interval, and a value that is not ``0`` or a positive finite
  number refuses to start. The same thread then runs the release
  authorization pass (``Workbench.release_authorize_pass``, F11 PR-5 D7):
  every newly registered release whose target and HealthProfile have
  exactly one calibrated policy is authorized once as ``system``, and a
  refusal is audited and final; and then the release anomaly pass
  (``Workbench.release_anomaly_pass``, F11 PR-4b R8): every anomalous
  release's anomaly ending is consumed into a new incident and queued Run,
  one event on the target's open incident, or a handoff. ``0`` turns all
  three off: no release is then observed (it stays pending until its
  deadline sweep ends it ``unknown``), and ending records are durable, so
  a stopped anomaly pass loses nothing and the next one consumes them.
* ``OPSPILOT_HEALTH_BIND``     the health listener's ``host:port``
  (``opspilot.health``, separate from the app on ``OPSPILOT_BIND``; default
  ``127.0.0.1:9464``, empty turns it off;
  any other value that cannot be bound refuses to start, so bare
  processes sharing a host need distinct values).

``hash-password`` and ``token-digest`` print the value to put in the
environment; the secret itself is read from stdin and never echoed. This
process runs no investigator: Runs stay ``queued`` until a worker
(``python -m opspilot.worker_main``) claims them. It runs as a bare
development process and as the ``web`` service of the Compose install
(``deploy/compose``); on start it only verifies that the schema is at the
Alembic head (migrations run beforehand as the owner: ``make migrate`` in
development, the ``migrate`` one-shot in Compose).
"""

from __future__ import annotations

import getpass
import logging
import math
import os
import sys
import threading
from pathlib import Path

from opspilot import health, schema
from opspilot.investigation.limits import RUN_WALL_SECONDS
from opspilot.knowledge import KnowledgeStore
from opspilot.observer.health_profile import (
    PROFILE_DIRECTORY,
    HealthProfile,
    HealthProfileError,
    load_health_profile,
)
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.schema import TARGET_IDENTITIES_ENV, TargetIdentityMissing
from opspilot.tools.profiles import select_profile
from opspilot.web.app import create_app
from opspilot.web.auth import AuthConfig, Authenticator, hash_password, token_digest
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.review import PostmortemReview
from opspilot.web.service import Workbench
from opspilot.web.store import (
    DurableClock,
    DurableIncidentStore,
    DurableWebLedger,
    MappingTargetRegistry,
)

_log = logging.getLogger(__name__)
#: On by default since #184 step 2 (r6 C2): the F6/F13 projections classify
#: the automatic authorization as the system's. ``0`` in the environment
#: turns the loop off.
SELF_HEAL_INTERVAL_SECONDS = 30.0


def _pairs(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise SystemExit("expected key=value entries")
        result[key] = value
    return result


def _run_seconds() -> float:
    raw = os.environ.get("OPSPILOT_RUN_SECONDS")
    if raw is None:
        return RUN_WALL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit("OPSPILOT_RUN_SECONDS must be a number") from None
    if not 0 < value <= RUN_WALL_SECONDS:
        raise SystemExit("OPSPILOT_RUN_SECONDS must be within the frozen wall")
    return value


def _self_heal_interval() -> float:
    raw = os.environ.get("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS")
    if raw is None:
        return SELF_HEAL_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(
            "OPSPILOT_SELF_HEAL_INTERVAL_SECONDS must be a number"
        ) from None
    if not math.isfinite(value) or value < 0:
        raise SystemExit("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS must be 0 or positive")
    return value


def self_heal_loop(
    workbench: Workbench, interval: float, stop: threading.Event
) -> None:
    """Run ``workbench.self_heal_pass()``,
    ``workbench.release_authorize_pass()`` and then
    ``workbench.release_anomaly_pass()`` every ``interval`` seconds until
    ``stop`` is set. Each pass catches its own failure, which is logged; the
    others still run and the next round runs on time: every decision is
    durable and keyed, so nothing is lost by retrying."""
    while not stop.wait(interval):
        try:
            workbench.self_heal_pass()
        except Exception:
            _log.exception("automatic observation pass failed")
        try:
            workbench.release_authorize_pass()
        except Exception:
            _log.exception("release authorize pass failed")
        try:
            workbench.release_anomaly_pass()
        except Exception:
            _log.exception("release anomaly pass failed")


def _target_registry() -> MappingTargetRegistry | None:
    """The deployment's target identities (migration 0004), used when a
    remediation is registered and to bind an Alertmanager alert by its
    labels; without the file every registration is refused for lack of
    identity, every alert is handed to a human and UI/event intake is
    unaffected."""
    path = os.environ.get(TARGET_IDENTITIES_ENV)
    if not path:
        return None
    try:
        return MappingTargetRegistry.from_file(path)
    except TargetIdentityMissing as exc:
        raise SystemExit(str(exc)) from None


def _health_profile() -> HealthProfile:
    """The HealthProfile "register remediation" fixes a session by; the
    shipped checkout profile unless ``OPSPILOT_HEALTH_PROFILE`` names a file."""
    path = Path(
        os.environ.get("OPSPILOT_HEALTH_PROFILE")
        or PROFILE_DIRECTORY / "otel-demo-checkout.json"
    )
    try:
        return load_health_profile(path)
    except HealthProfileError as exc:
        raise SystemExit(f"OPSPILOT_HEALTH_PROFILE: {exc}") from None


#: ``DurableStore.claimable_incidents``' listing condition, counted per Run
#: state: the ``opspilot_queue_depth`` gauge (F8-R2).
_QUEUE_DEPTH_SQL = (
    "SELECT count(*) FILTER (WHERE r.state='queued') AS queued, "
    "count(*) FILTER (WHERE r.state='running') AS running_lapsed "
    "FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id "
    "WHERE i.state NOT IN ('paused','cancelled','completed') AND i.mode='automatic' AND i.conclusion IS NULL "
    "AND r.state IN ('queued','running') AND r.deadline>clock_timestamp() "
    "AND (r.state='queued' OR r.lease_until IS NULL OR r.lease_until<=clock_timestamp())"
)


def _probe(store: DurableStore, head: str) -> tuple[bool, bool, dict[str, int] | None]:
    """``(pool_ok, schema_at_head, queue_depth)`` from one read-only
    transaction of the web's own pool; a refusal is ``pool_ok=False``."""
    try:
        with store.transaction(snapshot=True) as conn:
            try:
                at_head = schema.current_revision(conn) == head
            except schema.SchemaVersionUnreadable:
                # The failed statement aborted the transaction.
                return True, False, None
            row = conn.execute(_QUEUE_DEPTH_SQL).fetchone()
            depth = {
                "queued": int(row["queued"]) if row else 0,
                "running_lapsed": int(row["running_lapsed"]) if row else 0,
            }
            return True, at_head, depth
    except PersistenceError as exc:
        _log.warning("health probe refused code=%s", exc)
        return False, False, None


def _start_health(store: DurableStore) -> health.HealthServer | None:
    """The web's health listener (F8-R4): ``/readyz`` and ``/metrics`` share
    one probe transaction from the app's own pool, reused for
    ``PROBE_TTL_SECONDS``, so a request flood costs the workbench at most one
    connection per TTL; nothing is mounted on the app's routes."""
    head = schema.head_revision()
    probe = health.ttl_cache(lambda: _probe(store, head), health.PROBE_TTL_SECONDS)

    def readiness() -> health.Readiness:
        pool_ok, at_head, _ = probe()
        return health.web_readiness(schema_at_head=at_head, pool_ok=pool_ok)

    def metrics() -> str:
        _, at_head, depth = probe()
        return health.web_metrics(schema_at_head=at_head, queue_depth=depth)

    return health.serve_health(os.environ, readiness=readiness, metrics=metrics)


def _serve() -> int:
    import uvicorn

    dsn = os.environ.get("OPSPILOT_WEB_DSN")
    if not dsn:
        raise SystemExit("OPSPILOT_WEB_DSN is required")
    bind = os.environ.get("OPSPILOT_BIND", "127.0.0.1:8080")
    interval = _self_heal_interval()
    # Browsers send Origin on every form POST, so an empty allow-list would
    # refuse the dev UI itself; default to the bind address over plain http.
    origins = os.environ.get("OPSPILOT_ALLOWED_ORIGINS") or f"http://{bind}"
    config = AuthConfig(
        ui_users=_pairs(os.environ.get("OPSPILOT_UI_USERS", "")),
        event_tokens=_pairs(os.environ.get("OPSPILOT_EVENT_TOKENS", "")),
        auth_revision=os.environ.get("OPSPILOT_AUTH_REVISION", "dev"),
        allowed_origins=frozenset(filter(None, origins.split(","))),
    )
    store = DurableStore(dsn)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    profile = select_profile(os.environ)
    clock = DurableClock(store)
    knowledge = KnowledgeStore(dsn)
    try:
        knowledge_max_age = int(
            os.environ.get("OPSPILOT_KNOWLEDGE_MAX_AGE_SECONDS", "2592000")
        )
        if knowledge_max_age < 0:
            raise ValueError
    except ValueError:
        raise SystemExit(
            "OPSPILOT_KNOWLEDGE_MAX_AGE_SECONDS must be a nonnegative integer"
        ) from None
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        targets=_target_registry(),
        knowledge=None
        if os.environ.get("OPSPILOT_KNOWLEDGE_HINTS") == "off"
        else knowledge,
        knowledge_max_age_seconds=knowledge_max_age,
        health_profile=_health_profile(),
        # The same versions and tool face ``python -m opspilot.worker_main``
        # claims and runs with: a Run recorded under other versions is
        # blocked on claim (C3 §5), and without the face it has no input.
        run_versions=profile.versions(),
        tool_face=profile.face(clock),
        run_seconds=_run_seconds(),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        clock,
        review=PostmortemReview(knowledge=knowledge, events=events),
    )
    host, _, port = bind.rpartition(":")
    # Before any thread: a bind that cannot be served refuses to start.
    listener = _start_health(store)
    stop = threading.Event()
    healer = None
    if interval > 0:
        healer = threading.Thread(
            target=self_heal_loop,
            args=(workbench, interval, stop),
            name="opspilot-auto-observe",
            daemon=True,
        )
        healer.start()
    try:
        uvicorn.run(app, host=host or "127.0.0.1", port=int(port), proxy_headers=False)
    finally:
        stop.set()
        if listener is not None:
            listener.close()
        if healer is not None:
            # One pass is bounded by the statement and lock timeouts of its
            # transactions; a pass still running after this is abandoned
            # with the process (daemon thread), its transaction rolled back.
            healer.join(timeout=10)
        knowledge.close()
        store.close()
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "serve":
        return _serve()
    if command == "hash-password":
        print(hash_password(getpass.getpass("password: ")))
        return 0
    if command == "token-digest":
        print(token_digest(getpass.getpass("token: ")))
        return 0
    print("usage: python -m opspilot.web {serve|hash-password|token-digest}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
