"""Web manual-question client for the HolmesGPT track (#666 H-10, R8-2).

The question goes in as an engineer types it into the workbench: ``POST
/intake/ui``, form-encoded, a UI account's Basic credentials, the web's own
``Origin`` and ``Accept: application/json`` (``opspilot/web/app.py``). No
alert is invented. The target is the extended profile's one registered target
(``m0-otel-20260909``; its identity file is evaluation-side configuration).
The UI account is random per round and never written to evidence.
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opspilot.web.auth import hash_password
from scripts.itbench_lite.driver import HEALTH_PROFILE_ID, NAMESPACE
from scripts.itbench_lite.traces import require_loopback

TARGET_ID = "m0-otel-20260909"
CLUSTER_UID = "holmes-evals"
WORKLOAD = "checkout"
INTAKE_PATH = "/intake/ui"
TIMEOUT_S = 30


@dataclass(frozen=True)
class UiAccount:
    name: str
    password: str = field(repr=False)

    def users_env(self) -> str:
        """``OPSPILOT_UI_USERS``: ``name=<pbkdf2 hash>``."""
        return f"{self.name}={hash_password(self.password)}"

    def basic(self) -> str:
        pair = f"{self.name}:{self.password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")


def new_account() -> UiAccount:
    return UiAccount(f"holmes-eval-{secrets.token_hex(4)}", secrets.token_urlsafe(24))


def idempotency_key(round: str, qid: str, trial: int, chance: int) -> str:  # noqa: A002
    return f"holmes-{round}-{qid}-t{trial}-c{chance}"


def write_identities(path: Path) -> None:
    """The one target's identity (fields as ITBench's ``write_identities``)."""
    entry = {
        "integration_id": TARGET_ID,
        "cluster_uid": CLUSTER_UID,
        "namespace": NAMESPACE,
        "workload": WORKLOAD,
        "health_profile_id": HEALTH_PROFILE_ID,
        "match": {"version": 1, "labels": {"service": WORKLOAD}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({TARGET_ID: entry}, indent=1) + "\n")


@dataclass(frozen=True)
class SubmitResult:
    status: int
    incident_id: str | None
    run_id: str | None
    replayed: bool | None
    code: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def submit_question(
    web_url: str,
    account: UiAccount,
    *,
    question: str,
    key: str,
    target_id: str = TARGET_ID,
    opener: urllib.request.OpenerDirector | None = None,
) -> SubmitResult:
    """One manual question; ``code`` is the web's refusal code, if any."""
    base = web_url.rstrip("/")
    require_loopback(base + INTAKE_PATH)
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    body = urllib.parse.urlencode(
        {"target_id": target_id, "question": question, "idempotency_key": key}
    ).encode("utf-8")
    request = urllib.request.Request(
        base + INTAKE_PATH,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": origin,
            "Authorization": account.basic(),
        },
    )
    try:
        with (opener or _OPENER).open(request, timeout=TIMEOUT_S) as response:
            status, raw = int(response.status), response.read()
    except urllib.error.HTTPError as error:
        status, raw = int(error.code), error.read()
    try:
        reply = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        reply = None
    reply = reply if isinstance(reply, dict) else {}
    code = reply.get("code") or reply.get("error")
    return SubmitResult(
        status=status,
        incident_id=_str(reply.get("incident_id")),
        run_id=_str(reply.get("run_id")),
        replayed=reply.get("replayed")
        if isinstance(reply.get("replayed"), bool)
        else None,
        code=str(code) if isinstance(code, str) else None,
    )


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
