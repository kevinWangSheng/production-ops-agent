"""Client for SREGym's conductor API (#667; upstream ``conductor_api.py:228-270``).

Three endpoints: ``GET /status``, ``GET /get_app`` and ``POST /submit``. The
client only ever submits the diagnosis stage, at most once per instance; there
is no way to submit any other stage. Loopback addresses only, no system proxy,
no redirects. Errors carry a fixed ``code`` and never the response body.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from scripts.itbench_lite.traces import require_loopback

DIAGNOSIS = "diagnosis"
_WAITING = frozenset({"setup", None})
_PASSED = frozenset({"tearing_down", "done", "mitigation"})
#: Upstream ``conductor.py`` marks an abandoned attempt ``aborted``.
ABORTED = "aborted"


class ConductorError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AppInfo:
    app_name: str
    namespace: str
    namespaces: tuple[str, ...]
    descriptions: str
    stages: tuple[str, ...]


@dataclass(frozen=True)
class Status:
    stage: str | None
    remaining_s: float | None


def base_url_from_env(environ: Mapping[str, str]) -> str:
    """Same rule as upstream's drivers: ``API_HOSTNAME`` / ``API_PORT``."""
    host = environ.get("API_HOSTNAME") or "localhost"
    port = environ.get("API_PORT") or "8000"
    return f"http://{host}:{port}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class ConductorClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        try:
            if urllib.parse.urlsplit(base).scheme != "http":
                raise ValueError("http only")
            require_loopback(base)
        except ValueError:
            raise ConductorError("CONDUCTOR_NOT_LOOPBACK") from None
        self.base_url = base
        self.timeout_s = timeout_s
        self._opener = opener or _opener()
        self._submitted = False

    # -- transport -------------------------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: bytes | None = None
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers
        )
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                status, raw = int(response.status), response.read()
        except urllib.error.HTTPError as error:
            status, raw = int(error.code), error.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            raise ConductorError("CONDUCTOR_UNREACHABLE") from None
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            payload = None
        return status, payload

    def _get(self, path: str) -> dict[str, Any]:
        status, payload = self._request("GET", path)
        if status == 400:
            raise ConductorError("CONDUCTOR_NOT_STARTED")
        if status != 200 or not isinstance(payload, dict):
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        return payload

    # -- endpoints -------------------------------------------------------------------------------

    def status(self) -> Status:
        payload = self._get("/status")
        if "stage" not in payload:
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        stage = payload["stage"]
        remaining = payload.get("agent_remaining_seconds")
        if stage is not None and not isinstance(stage, str):
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        if remaining is not None and (
            isinstance(remaining, bool) or not isinstance(remaining, int | float)
        ):
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        return Status(stage, None if remaining is None else float(remaining))

    def get_app(self) -> AppInfo:
        payload = self._get("/get_app")
        app_name = payload.get("app_name")
        namespace = payload.get("namespace")
        namespaces = payload.get("namespaces")
        descriptions = payload.get("descriptions")
        stages = payload.get("stages")
        if not isinstance(app_name, str) or not isinstance(namespace, str):
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        if namespaces is None:
            namespaces = [namespace]
        if not isinstance(namespaces, list) or not all(
            isinstance(n, str) for n in namespaces
        ):
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        if not isinstance(descriptions, str):
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        if not isinstance(stages, list) or not all(isinstance(s, str) for s in stages):
            raise ConductorError("CONDUCTOR_BAD_RESPONSE")
        if DIAGNOSIS not in stages:
            raise ConductorError("NO_DIAGNOSIS_STAGE")
        return AppInfo(
            app_name=app_name,
            namespace=namespace,
            namespaces=tuple(namespaces),
            descriptions=descriptions,
            stages=tuple(stages),
        )

    def submit_diagnosis(self, text: str) -> int:
        """``POST /submit`` with exactly ``{"stage": "diagnosis", "solution": text}``."""
        if self._submitted:
            raise ConductorError("ALREADY_SUBMITTED")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        self._submitted = True
        body = json.dumps({"stage": DIAGNOSIS, "solution": text}).encode("utf-8")
        status, _ = self._request("POST", "/submit", body)
        if status == 200:
            return status
        if status == 409:
            raise ConductorError("SUBMIT_REJECTED")
        if status == 503:
            raise ConductorError("SUBMIT_UNAVAILABLE")
        if status in (400, 422):
            raise ConductorError("SUBMIT_INVALID")
        raise ConductorError("CONDUCTOR_BAD_RESPONSE")

    # -- waiting ---------------------------------------------------------------------------------

    def wait_for_diagnosis(
        self,
        *,
        timeout_s: float,
        poll_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Status:
        """Poll until the stage is ``diagnosis``. ``setup``/None and
        unreachable or not-started conductors keep waiting; a passed stage
        raises ``STAGE_PASSED``; ``aborted`` raises ``CONDUCTOR_ABORTED``."""
        deadline = clock() + timeout_s
        while True:
            try:
                current = self.status()
            except ConductorError as error:
                if error.code not in ("CONDUCTOR_UNREACHABLE", "CONDUCTOR_NOT_STARTED"):
                    raise
            else:
                if current.stage == ABORTED:
                    raise ConductorError("CONDUCTOR_ABORTED")
                if current.stage == DIAGNOSIS:
                    return current
                if current.stage in _PASSED:
                    raise ConductorError("STAGE_PASSED")
                if current.stage not in _WAITING:
                    raise ConductorError("CONDUCTOR_BAD_RESPONSE")
            if clock() >= deadline:
                raise ConductorError("STAGE_WAIT_TIMEOUT")
            sleep(poll_s)

    def wait_for_done(
        self,
        *,
        timeout_s: float,
        poll_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Status:
        """Poll until ``done``; a conductor that answers "not started" has
        already closed the attempt and counts as done; ``aborted`` raises
        ``CONDUCTOR_ABORTED`` at once."""
        deadline = clock() + timeout_s
        while True:
            try:
                current = self.status()
            except ConductorError as error:
                if error.code == "CONDUCTOR_NOT_STARTED":
                    return Status("done", None)
                if error.code != "CONDUCTOR_UNREACHABLE":
                    raise
            else:
                if current.stage == ABORTED:
                    raise ConductorError("CONDUCTOR_ABORTED")
                if current.stage == "done":
                    return current
            if clock() >= deadline:
                raise ConductorError("STAGE_WAIT_TIMEOUT")
            sleep(poll_s)
