"""Development entry point: ``python -m opspilot.web serve`` behind a trusted proxy.

Configuration comes from the environment only, and only as hashes:

* ``OPSPILOT_DSN``            PostgreSQL DSN of the business-state authority.
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
  .load_target_identities``); read when a remediation is registered,
  optional (without it registrations are refused for lack of identity).
* ``OPSPILOT_HEALTH_PROFILE``  path of the HealthProfile JSON the
  "register remediation" action fixes an observation session by (default:
  the shipped ``otel-demo-checkout`` profile).

``hash-password`` and ``token-digest`` print the value to put in the
environment; the secret itself is read from stdin and never echoed. This
process runs no investigator: Runs stay ``queued`` until a worker
(``python -m opspilot.worker_main``) claims them. It is not a deployment artifact and installs schema on start, which
a production path must not do.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from typing import cast

from opspilot.investigation.limits import RUN_WALL_SECONDS
from opspilot.knowledge import KnowledgeStore
from opspilot.observer.health_profile import (
    PROFILE_DIRECTORY,
    HealthProfile,
    HealthProfileError,
    load_health_profile,
)
from opspilot.persistence import DurableStore
from opspilot.schema import TARGET_IDENTITIES_ENV, TargetIdentityMissing
from opspilot.tools.profiles import select_profile
from opspilot.web.app import create_app
from opspilot.web.auth import AuthConfig, Authenticator, hash_password, token_digest
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.review import KnowledgeReview, PostmortemReview
from opspilot.web.service import Workbench
from opspilot.web.store import (
    DurableClock,
    DurableIncidentStore,
    DurableWebLedger,
    MappingTargetRegistry,
)


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


def _target_registry() -> MappingTargetRegistry | None:
    """The deployment's target identities (migration 0004), used only when a
    remediation is registered; without the file every registration is
    refused for lack of identity, intake is unaffected."""
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


def _serve() -> int:
    import uvicorn

    dsn = os.environ.get("OPSPILOT_DSN")
    if not dsn:
        raise SystemExit("OPSPILOT_DSN is required")
    bind = os.environ.get("OPSPILOT_BIND", "127.0.0.1:8080")
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
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        targets=_target_registry(),
        health_profile=_health_profile(),
        # The same versions and tool face ``python -m opspilot.worker_main``
        # claims and runs with: a Run recorded under other versions is
        # blocked on claim (C3 §5), and without the face it has no input.
        run_versions=profile.versions(),
        tool_face=profile.face(clock),
        run_seconds=_run_seconds(),
    )
    knowledge = KnowledgeStore(dsn)
    app = create_app(
        workbench,
        Authenticator(config),
        clock,
        # TODO(M1-03 step 2 merge): drop the cast once ``incident_postmortem``
        # (D26) is on main; this branch is merged after step 2.
        review=PostmortemReview(
            knowledge=cast(KnowledgeReview, knowledge), events=events
        ),
    )
    host, _, port = bind.rpartition(":")
    try:
        uvicorn.run(app, host=host or "127.0.0.1", port=int(port), proxy_headers=False)
    finally:
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
