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

from opspilot.investigation.limits import RUN_WALL_SECONDS
from opspilot.persistence import DurableStore
from opspilot.tools.profiles import select_profile
from opspilot.web.app import create_app
from opspilot.web.auth import AuthConfig, Authenticator, hash_password, token_digest
from opspilot.web.events import DurableEventLog
from opspilot.web.evidence import DurableEvidenceStore
from opspilot.web.service import Workbench
from opspilot.web.store import DurableClock, DurableIncidentStore, DurableWebLedger


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
        # The same versions and tool face ``python -m opspilot.worker_main``
        # claims and runs with: a Run recorded under other versions is
        # blocked on claim (C3 §5), and without the face it has no input.
        run_versions=profile.versions(),
        tool_face=profile.face(clock),
        run_seconds=_run_seconds(),
    )
    app = create_app(workbench, Authenticator(config), clock)
    host, _, port = bind.rpartition(":")
    uvicorn.run(app, host=host or "127.0.0.1", port=int(port), proxy_headers=False)
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
