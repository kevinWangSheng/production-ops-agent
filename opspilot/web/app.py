"""FastAPI/Jinja workbench with the two authenticated intake channels.

Routes (C3 section 9): incident list and detail, the SSE progress stream
with cursor resume, the UI intake form, the token-authenticated event
intake, a human-control endpoint, evidence read-back and the release
observation list, detail and pause/resume/cancel (F11 PR-5a). Every handler
authenticates first and parses the path second, so a caller without
credentials learns nothing about what exists.

Request bodies are parsed here without ``Request.form()``/``Request.json()``:
the former needs an extra dependency and the latter would put a
model-shaped ``.json()`` call in product code, which the architecture test
reserves for the sanitizer.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from opspilot.alertmanager import parse_payload
from opspilot.domain.base import DomainError
from opspilot.domain.intake import delivery_key
from opspilot.intake import IntakeEnvelope, IntakeRequest, Principal, verify_channel
from opspilot.knowledge.contract import ReviewCommand
from opspilot.observation.releases import ReleaseStore
from opspilot.observer.health_profile import canonical_content
from opspilot.persistence import PersistenceError
from opspilot.redaction import redact_json_bytes, redact_payload
from opspilot.release_intake import (
    REJECTION_STATUS,
    RELEASE_ACTOR,
    RELEASE_MAX_BODY_BYTES,
    RELEASE_MIN_TRACKING_SECONDS,
    ReleaseIntakeError,
    parse_release,
    resolve_target,
)
from opspilot.tools.executor import Clock
from opspilot.web.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    Authenticator,
    AuthError,
)
from opspilot.web.events import CursorTooOld
from opspilot.web.incident_list import parse_query
from opspilot.web.incident_view import LIVE_KINDS, handoff_view, page_view
from opspilot.web.review import EVENT_KINDS, PostmortemReview, ReviewError
from opspilot.web.runbooks import MAX_FORM_BYTES as RUNBOOK_FORM_BYTES
from opspilot.web.runbooks import RunbookReview, RunbookStoreSurface
from opspilot.web.service import (
    MESSAGE_ACTIONS,
    Workbench,
    WorkbenchError,
    available_actions,
)

MAX_BODY_BYTES = 64 * 1024
#: One Alertmanager webhook carries a whole group of alerts (each recorded up
#: to 16 KiB); a 4xx is never retried by Alertmanager, so the cap is wider.
ALERTMANAGER_MAX_BODY_BYTES = 1024 * 1024
_TEMPLATES = Path(__file__).with_name("templates")
_FORM = "application/x-www-form-urlencoded"
#: Refuse to be framed, on every response. A framed page submits the control
#: forms with the workbench's own Origin, which ``check_origin()`` rightly
#: accepts, so UI redressing would reach pause/cancel/follow-up (codex security
#: review, PR #33). Only ``frame-ancestors``: the templates use inline script.
_ANTI_FRAMING_HEADERS = (
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
)


class _AntiFraming:
    """Raw ASGI middleware: adds the headers to every ``http.response.start``.

    Raw rather than ``BaseHTTPMiddleware`` so the SSE stream is passed through
    unbuffered and Starlette's own 404/405 and the exception handlers are
    covered alike.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                names = {name.lower() for name, _ in headers}
                headers.extend(h for h in _ANTI_FRAMING_HEADERS if h[0] not in names)
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


_AUTH_STATUS = {
    "MISSING_CREDENTIALS": 401,
    "INVALID_CREDENTIALS": 401,
    "INVALID_SESSION": 401,
    "ORIGIN_REJECTED": 403,
}
#: The login form's one refusal, whatever was wrong (#682): no enumeration.
LOGIN_FAILED = "Wrong user name or password"
_EVENT_PATHS = ("/intake/events", "/intake/alertmanager", "/intake/version-changes")
#: The sentence an HTML page shows for a refused page GET; the code stays
#: in small secondary text. JSON clients still get the JSON body.
_PAGE_ERRORS = {
    "UNKNOWN_INCIDENT": "This incident does not exist.",
    "UNKNOWN_RELEASE": "This version change does not exist.",
    "UNKNOWN_EVIDENCE": "This evidence record does not exist.",
    "UNKNOWN_TARGET": "This target is not registered.",
    "NOT_FOUND": "This page does not exist.",
    "CHANNEL_MISMATCH": "These credentials cannot open the workbench.",
    "MISSING_CREDENTIALS": "Sign in to continue.",
    "INVALID_CREDENTIALS": "Sign in to continue.",
    "INVALID_SESSION": "Your session has ended. Sign in again.",
}
_FRAMEWORK_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
_STATUS_ERRORS = {
    400: "This address is not valid.",
    401: "Sign in to continue.",
    403: "You cannot open this page.",
    404: "This page does not exist.",
    405: "This page does not exist.",
    409: "This page cannot be shown in its current state. Reload and try again.",
    503: "The workbench cannot reach its storage right now. Try again shortly.",
}
_WORKBENCH_STATUS = {
    "UNKNOWN_INCIDENT": 404,
    "UNKNOWN_TARGET": 404,
    "UNKNOWN_RELEASE": 404,
    "INVALID_INPUT": 400,
    "INVALID_ACTION": 400,
    "TEXT_REQUIRED": 400,
    "TEXT_NOT_ALLOWED": 400,
    "REVISION_REQUIRED": 400,
    "REVISION_NOT_ALLOWED": 400,
    "TARGET_REQUIRED": 400,
    "TARGET_NOT_ALLOWED": 400,
    "INTO_INCIDENT_REQUIRED": 400,
    "INTO_INCIDENT_NOT_ALLOWED": 400,
    "TARGET_UNCHANGED": 409,
    "HEALTH_PROFILE_REQUIRED": 409,
    "SCOPE_SUSPENDED": 409,
    "TARGET_MISMATCH": 409,
    "TARGET_IDENTITY_MISSING": 409,
    "HEALTH_PROFILE_TARGET_MISMATCH": 409,
    "INTAKE_KEY_CONFLICT": 409,
    "HANDOFF_ONLY": 409,
    # Workbench contract D6/D7b/D10: a message the investigation cannot take.
    "INVESTIGATION_NOT_AVAILABLE": 409,
    "MERGED": 409,
    "CONTROL_KEY_CONFLICT": 409,
    "CONTROL_CONFLICT": 409,
    "ILLEGAL_TRANSITION": 409,
    "REOPEN_REQUIRED": 409,
    "IDENTITY_CONFLICT": 409,
    "UNKNOWN_IDENTITY": 404,
    "STORAGE_UNAVAILABLE": 503,
    "RETRY": 503,
    "TIMEOUT": 503,
    # Workbench P5 (feedback, F8): no report on the page to rate
    "NO_REPORT": 409,
    "REPORT_CHANGED": 409,
    "FEEDBACK_UNAVAILABLE": 503,
    # Workbench P7 (Chat panel)
    "CHAT_RATE_LIMITED": 429,
    "CHAT_BUSY": 409,
    "CHAT_KEY_CONFLICT": 409,
    "CHAT_NOT_STEER": 409,
    "CHAT_UNKNOWN_SESSION": 404,
    "CHAT_UNKNOWN_TURN": 404,
    "CHAT_UNAVAILABLE": 503,
}


#: F11 PR-5a D2: a release control refused for the release's current state
#: is a conflict (reload and decide again); a calibration, target or profile
#: refusal is final for the request (422). Kept apart from the incident
#: table, where TARGET_* codes are 409.
_RELEASE_CONFLICTS = frozenset(
    {
        "CONTROL_CONFLICT",
        "RELEASE_TERMINAL",
        "ALREADY_PAUSED",
        "OBSERVATION_ALREADY_AUTHORIZED",
        "REAUTHORIZATION_REQUIRED",
        "RELEASE_PAUSED",
        "SCOPE_SUSPENDED",
        "RELEASE_DEADLINE_EXPIRED",
        "RELEASE_SAMPLE_BUDGET_EXHAUSTED",
        "IDEMPOTENCY_CONFLICT",
    }
)
_RELEASE_STATUS = {
    "UNKNOWN_RELEASE": 404,
    "NOT_FOUND": 404,
    "INVALID_INPUT": 400,
    "INVALID_RELEASE_POLICY": 422,
}


def _release_status(code: str, default: int) -> int:
    if code in _RELEASE_CONFLICTS:
        return 409
    if code.startswith(("RELEASE_POLICY_", "TARGET_", "HEALTH_PROFILE_")):
        return 422
    return _RELEASE_STATUS.get(code, default)


#: R3: a conflict asks for a reload, a refusal is final for this request.
_REVIEW_STATUS = {
    "conflict": 409,
    "refused": 422,
    "invalid": 400,
    "not_found": 404,
    "unavailable": 503,
}
_ENTRY_GENERATION_FIELD = "entry_generation:"


class _Refusal(Exception):
    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


def create_app(
    workbench: Workbench,
    authenticator: Authenticator,
    clock: Clock,
    *,
    sse_poll_seconds: float = 0.5,
    sse_idle_seconds: float | None = 30.0,
    sse_repair_seconds: float = 5.0,
    review: PostmortemReview | None = None,
    releases: ReleaseStore | None = None,
    runbooks: RunbookStoreSurface | None = None,
) -> FastAPI:
    # No unauthenticated schema surface: /openapi.json would list every route.
    app = FastAPI(
        title="OpsPilot workbench", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(_AntiFraming)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )

    def render(name: str, **context: Any) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(**context))

    def set_session(response: Response, value: str | None) -> None:
        options: dict[str, Any] = {
            "path": "/",
            "httponly": True,
            "samesite": "lax",
            "secure": authenticator.secure_cookie,
        }
        if value is None:
            response.delete_cookie(SESSION_COOKIE, **options)
        else:
            response.set_cookie(
                SESSION_COOKIE,
                value,
                max_age=int(SESSION_MAX_AGE.total_seconds()),
                **options,
            )

    def error_page(request: Request, exc: _Refusal) -> HTMLResponse:
        message = _PAGE_ERRORS.get(exc.code) or _STATUS_ERRORS.get(
            exc.status, "This page could not be shown."
        )
        response = render(
            "error.html",
            principal=getattr(request.state, "principal", None),
            message=message,
            code=exc.code,
            sign_in=exc.status == 401,
        )
        response.status_code = exc.status
        return response

    @app.exception_handler(StarletteHTTPException)
    async def framework_error(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        """Starlette's own 404/405 (an unknown path or method): the HTML
        error page for a page GET, FastAPI's JSON otherwise."""
        if request.method == "GET" and wants_html(request):
            response = error_page(
                request,
                _Refusal(
                    exc.status_code,
                    _FRAMEWORK_CODES.get(exc.status_code, f"HTTP_{exc.status_code}"),
                ),
            )
            for name, value in (exc.headers or {}).items():
                response.headers[name] = value
            return response
        return await http_exception_handler(request, exc)

    @app.exception_handler(_Refusal)
    async def refused(request: Request, exc: _Refusal) -> Response:
        event_path = request.url.path.startswith(_EVENT_PATHS)
        if (
            wants_html(request)
            and not event_path
            and (request.method == "GET" or exc.status == 401)
        ):
            # A browser: never the Basic dialog. An unauthenticated page
            # GET goes to the login form and comes back afterwards; a
            # refused page GET (or a form post whose session ended) gets a
            # sentence, not JSON.
            if exc.status == 401 and request.method == "GET":
                target = request.url.path
                if request.url.query:
                    target += "?" + request.url.query
                response: Response = RedirectResponse(
                    "/login?next=" + quote(target, safe=""), status_code=302
                )
            else:
                response = error_page(request, exc)
            if exc.code == "INVALID_SESSION":
                set_session(response, None)
            return response
        headers = {}
        if exc.status == 401 and not event_path:
            headers["WWW-Authenticate"] = 'Basic realm="opspilot"'
        return JSONResponse(
            {"code": exc.code, **exc.extra}, status_code=exc.status, headers=headers
        )

    async def ui(request: Request) -> Principal:
        cookie = request.cookies.get(SESSION_COOKIE)
        try:
            principal: Principal | None = None
            if cookie is not None:
                # A valid session wins over a Basic header the browser may
                # still cache for another user: the form login is who is
                # signed in. The database clock, as every product time.
                now = await asyncio.to_thread(clock.now)
                try:
                    principal = authenticator.session_principal(cookie, now)
                except AuthError:
                    if request.headers.get("authorization") is None:
                        raise
            if principal is None:
                principal = authenticator.ui_principal(request.headers)
            principal = verify_channel(principal, expected="ui_basic")
        except AuthError as exc:
            raise _Refusal(_AUTH_STATUS.get(exc.code, 401), exc.code) from None
        except DomainError:
            raise _Refusal(403, "CHANNEL_MISMATCH") from None
        except PersistenceError:
            raise _Refusal(503, "STORAGE_UNAVAILABLE") from None
        request.state.principal = principal
        return principal

    async def ui_mutation(request: Request) -> Principal:
        principal = await ui(request)
        try:
            authenticator.check_origin(request.headers)
        except AuthError as exc:
            raise _Refusal(403, exc.code) from None
        return principal

    async def event_channel(request: Request) -> Principal:
        try:
            principal = authenticator.event_principal(request.headers)
            return verify_channel(principal, expected="event_token")
        except AuthError as exc:
            raise _Refusal(_AUTH_STATUS.get(exc.code, 401), exc.code) from None
        except DomainError:
            raise _Refusal(403, "CHANNEL_MISMATCH") from None

    async def generic_event_channel(request: Request) -> Principal:
        """An event principal for the generic intake routes. The release
        actor's token is shared in ``OPSPILOT_EVENT_TOKENS`` but grants only
        ``/intake/version-changes`` (F11 PR-3 U4); refused before the body
        is read, as that route refuses other actors."""
        principal = await event_channel(request)
        if principal.actor_id == RELEASE_ACTOR:
            raise _Refusal(403, "RELEASE_ACTOR_FORBIDDEN")
        return principal

    async def body_of(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise _Refusal(413, "BODY_TOO_LARGE")
        # Stream so an undeclared or chunked body is capped, not buffered.
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise _Refusal(413, "BODY_TOO_LARGE")
            chunks.append(chunk)
        return b"".join(chunks)

    async def form_of(request: Request, limit: int = MAX_BODY_BYTES) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        if content_type != _FORM:
            raise _Refusal(415, "FORM_REQUIRED")
        body = await body_of(request, limit)
        try:
            fields = parse_qs(
                body.decode("utf-8"), keep_blank_values=True, strict_parsing=False
            )
        except UnicodeDecodeError:
            raise _Refusal(400, "INVALID_INPUT") from None
        return {key: values[0] for key, values in fields.items() if values}

    async def json_of(request: Request) -> dict[str, Any]:
        body = await body_of(request)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _Refusal(400, "INVALID_JSON") from None
        if not isinstance(payload, dict):
            raise _Refusal(400, "INVALID_JSON")
        return payload

    def incident_of(raw: str) -> UUID:
        try:
            return UUID(raw)
        except ValueError:
            raise _Refusal(404, "UNKNOWN_INCIDENT") from None

    def wants_html(request: Request) -> bool:
        return request.headers.get("accept", "").startswith("text/html")

    async def in_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except WorkbenchError as exc:
            extra = {}
            if exc.current_generation is not None:
                extra["current_generation"] = exc.current_generation
            raise _Refusal(_WORKBENCH_STATUS.get(exc.code, 409), exc.code, **extra)
        except PersistenceError as exc:
            code = str(exc)
            raise _Refusal(_WORKBENCH_STATUS.get(code, 503), code) from None
        except CursorTooOld:
            raise _Refusal(409, "CURSOR_TOO_OLD") from None

    async def envelope_from(
        principal: Principal, fields: Mapping[str, Any]
    ) -> IntakeEnvelope:
        try:
            request = IntakeRequest(
                target_id=_text(fields, "target_id"),
                question=_text(fields, "question"),
                idempotency_key=_text(fields, "idempotency_key"),
            )
            received = await asyncio.to_thread(clock.now)
            return IntakeEnvelope(
                request_id=str(uuid4()),
                principal=principal,
                request=request,
                received_at=received,
            )
        except (ValidationError, ValueError):
            # Never render the failure: it may quote the rejected input.
            raise _Refusal(400, "INVALID_INPUT") from None

    async def reviewed(func: Any, /, *args: Any) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except ReviewError as exc:
            raise _Refusal(
                _REVIEW_STATUS[exc.error_class], exc.code, **_review_extra(exc)
            ) from None

    def review_of() -> PostmortemReview:
        if review is None:
            raise _Refusal(404, "NOT_FOUND")
        return review

    # C2a (#566): the registry's targets, or any target an intake accepts
    # when no registry is configured
    runbook_review = (
        None
        if runbooks is None
        else RunbookReview(
            store=runbooks,
            target_keys=lambda: (
                None if workbench.targets is None else workbench.targets.keys()
            ),
        )
    )

    def runbooks_of() -> RunbookReview:
        if runbook_review is None:
            raise _Refusal(404, "NOT_FOUND")
        return runbook_review

    # -- sign-in (#682) ----------------------------------------------------

    def login_page(target: str, *, error: str | None, status: int) -> HTMLResponse:
        response = render(
            "login.html",
            principal=None,
            next=target,
            error=error,
            sessions_enabled=authenticator.sessions_enabled,
        )
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        return login_page(
            safe_next(request.query_params.get("next")), error=None, status=200
        )

    @app.post("/login")
    async def login(request: Request) -> Response:
        try:
            authenticator.check_origin(request.headers)
        except AuthError as exc:
            raise _Refusal(403, exc.code) from None
        if not authenticator.sessions_enabled:
            raise _Refusal(503, "SESSIONS_DISABLED")
        fields = await form_of(request)
        target = safe_next(fields.get("next"))
        try:
            principal = await asyncio.to_thread(
                authenticator.login,
                fields.get("username", ""),
                fields.get("password", ""),
            )
            principal = verify_channel(principal, expected="ui_basic")
        except (AuthError, DomainError):
            # Never the user name or password back, and no WWW-Authenticate.
            return login_page(target, error=LOGIN_FAILED, status=401)
        try:
            issued_at = await asyncio.to_thread(clock.now)
        except PersistenceError:
            raise _Refusal(503, "STORAGE_UNAVAILABLE") from None
        response = RedirectResponse(target, status_code=303)
        set_session(response, authenticator.issue_session(principal, issued_at))
        return response

    @app.get("/logout", response_class=HTMLResponse)
    async def logout_confirm(request: Request) -> Response:
        # A link cannot carry Origin; the state change is the POST below.
        # Without a valid session there is nothing to end: like every page,
        # an HTML GET goes to /login?next=/logout.
        principal = await ui(request)
        response = render("logout.html", principal=principal)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        try:
            authenticator.check_origin(request.headers)
        except AuthError as exc:
            raise _Refusal(403, exc.code) from None
        response = RedirectResponse("/login", status_code=303)
        set_session(response, None)
        return response

    # -- pages ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        principal = await ui(request)
        query = parse_query(request.query_params)
        listing = await in_thread(workbench.incident_list, **query)
        return render("index.html", principal=principal, listing=listing)

    @app.get("/controls")
    async def controls_page(request: Request) -> Response:
        principal = await ui(request)
        state = await in_thread(workbench.controls)
        if wants_html(request):
            return render(
                "controls.html",
                principal=principal,
                state=state,
                form_nonce=uuid4().hex,
            )
        return JSONResponse(state)

    @app.post("/controls/suspension")
    async def suspension(request: Request) -> Response:
        principal = await ui_mutation(request)
        fields = await form_of(request)
        generation = fields.get("expected_generation", "")
        if not generation.isascii() or not generation.isdigit() or len(generation) > 19:
            raise _Refusal(400, "INVALID_INPUT")
        if fields.get("suspended") not in {"true", "false"}:
            raise _Refusal(400, "INVALID_INPUT")
        result = await in_thread(
            workbench.suspension,
            scope=fields.get("scope", ""),
            target_id=fields.get("target_id"),
            suspended=fields["suspended"] == "true",
            expected_generation=int(generation),
            idempotency_key=fields.get("idempotency_key", ""),
            actor_id=principal.actor_id,
        )
        if wants_html(request):
            return RedirectResponse("/controls", status_code=303)
        return JSONResponse(result)

    async def incident_context(
        principal: Principal, subject: UUID, snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        postmortem = None
        if review is not None:
            postmortem = await reviewed(review.incident_view, subject)
        release_links = snapshot.get("release_links")
        if release_links is None:  # a handoff-only snapshot does not read them
            release_links = await in_thread(workbench.release_links_for, subject)
        # Workbench P5: the incident's feedback card (F10).
        feedback = await in_thread(workbench.feedback_for, subject, snapshot)
        actions = available_actions(snapshot)
        return {
            "feedback": feedback,
            "principal": principal,
            "snapshot": snapshot,
            "release_links": release_links,
            "postmortem": postmortem,
            "postmortem_kinds": EVENT_KINDS,
            "live_kinds": LIVE_KINDS,
            "actions": actions,
            # #572: display-only derivations from the same snapshot.
            "view": page_view(snapshot, postmortem, actions, message_available=True),
            # One fresh key per rendered form: two operators on the same page
            # state must not share an idempotency key.
            "form_nonce": uuid4().hex,
        }

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    async def incident_page(request: Request, incident_id: str) -> Response:
        principal = await ui(request)
        subject = incident_of(incident_id)
        snapshot = await in_thread(workbench.snapshot, subject)
        if snapshot.get("handoff_only"):
            return render(
                "incident_handoff.html",
                principal=principal,
                snapshot=snapshot,
                view=handoff_view(snapshot),
            )
        return render(
            "incident.html", **await incident_context(principal, subject, snapshot)
        )

    @app.get("/incidents/{incident_id}/live", response_class=HTMLResponse)
    async def incident_live(request: Request, incident_id: str) -> Response:
        """The incident page's changing regions alone (workbench P3-14): the
        same login, snapshot and projection as the page, which swaps them
        in place on every event instead of reloading."""
        principal = await ui(request)
        subject = incident_of(incident_id)
        snapshot = await in_thread(workbench.snapshot, subject)
        if snapshot.get("handoff_only"):
            response = HTMLResponse('<div id="live" data-reload="1"></div>')
        else:
            response = render(
                "incident_live.html",
                **await incident_context(principal, subject, snapshot),
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- postmortem review (M1-03 step 3) ----------------------------------

    async def render_version(
        principal: Principal,
        page: Mapping[str, Any],
        *,
        error: Any = None,
        status: int = 200,
    ) -> HTMLResponse:
        # UI redesign 3.3: the heading names what the incident is about, read
        # like its list row (one single-row query).
        incident = await in_thread(workbench.incident_subject, page["incident_id"])
        # Workbench P5 F11 (user, option A): the reviewer sees the
        # incident's feedback here; it is never sent to the drafting model.
        feedback = await in_thread(workbench.feedback_for, page["incident_id"])
        response = render(
            "postmortem.html",
            principal=principal,
            page=page,
            incident=incident,
            feedback=feedback,
            error=error,
            postmortem_kinds=EVENT_KINDS,
            form_nonce=uuid4().hex,
        )
        response.status_code = status
        return response

    def render_entry(
        principal: Principal,
        page: Mapping[str, Any],
        *,
        error: Any = None,
        status: int = 200,
    ) -> HTMLResponse:
        response = render(
            "knowledge.html",
            principal=principal,
            page=page,
            error=error,
            form_nonce=uuid4().hex,
        )
        response.status_code = status
        return response

    @app.get(
        "/postmortems/{postmortem_id}/versions/{version}", response_class=HTMLResponse
    )
    async def version_page(
        request: Request, postmortem_id: str, version: str
    ) -> Response:
        principal = await ui(request)
        subject, number = _postmortem_path(postmortem_id, version)
        page = await reviewed(review_of().version_page, subject, number)
        return await render_version(principal, page)

    @app.get("/knowledge/{entry_id}", response_class=HTMLResponse)
    async def entry_page(request: Request, entry_id: str) -> Response:
        principal = await ui(request)
        page = await reviewed(review_of().entry_page, _uuid_path(entry_id))
        return render_entry(principal, page)

    @app.post("/postmortems/{postmortem_id}/versions/{version}/review")
    async def review_version(
        request: Request, postmortem_id: str, version: str
    ) -> Response:
        principal = await ui_mutation(request)
        service = review_of()
        subject, number = _postmortem_path(postmortem_id, version)
        fields = await form_of(request)
        action = fields.get("action", "")
        if action not in ("approve", "supersede", "reject", "return"):
            raise _invalid("action")
        try:
            entries = {
                UUID(name[len(_ENTRY_GENERATION_FIELD) :]): _generation(value)
                for name, value in fields.items()
                if name.startswith(_ENTRY_GENERATION_FIELD)
            }
        except ValueError:
            raise _invalid("entry_generations") from None
        reason = fields.get("reason")
        command = ReviewCommand(
            action=action,  # type: ignore[arg-type]
            idempotency_key=fields.get("idempotency_key", ""),
            expected_generation=_expected(fields),
            postmortem_id=subject,
            version=number,
            reason=reason if reason else None,
            entry_generations=entries,
        )
        try:
            result = await asyncio.to_thread(
                service.review, command, principal.actor_id
            )
        except ReviewError as exc:
            if not wants_html(request):
                raise _Refusal(
                    _REVIEW_STATUS[exc.error_class], exc.code, **_review_extra(exc)
                ) from None
            page = await reviewed(service.version_page, subject, number)
            return await render_version(
                principal, page, error=exc, status=_REVIEW_STATUS[exc.error_class]
            )
        if wants_html(request):
            return RedirectResponse(
                f"/postmortems/{subject}/versions/{number}", status_code=303
            )
        return JSONResponse(result)

    @app.post("/knowledge/{entry_id}/revoke")
    async def revoke_entry(request: Request, entry_id: str) -> Response:
        principal = await ui_mutation(request)
        service = review_of()
        subject = _uuid_path(entry_id)
        fields = await form_of(request)
        raw_revision = fields.get("revision", "")
        reason = fields.get("reason")
        command = ReviewCommand(
            action="revoke",
            idempotency_key=fields.get("idempotency_key", ""),
            expected_generation=_expected(fields),
            entry_id=subject,
            revision=int(raw_revision) if raw_revision.isdigit() else 0,
            reason=reason if reason else None,
        )
        try:
            result = await asyncio.to_thread(
                service.review, command, principal.actor_id
            )
        except ReviewError as exc:
            if not wants_html(request):
                raise _Refusal(
                    _REVIEW_STATUS[exc.error_class], exc.code, **_review_extra(exc)
                ) from None
            page = await reviewed(service.entry_page, subject)
            return render_entry(
                principal, page, error=exc, status=_REVIEW_STATUS[exc.error_class]
            )
        if wants_html(request):
            return RedirectResponse(f"/knowledge/{subject}", status_code=303)
        return JSONResponse(result)

    # -- imported runbooks (C2a, #566) ----------------------------------------

    def render_runbooks(
        principal: Principal,
        template: str,
        *,
        error: Any = None,
        status: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        response = render(
            template,
            principal=principal,
            error=error,
            form_nonce=uuid4().hex,
            **context,
        )
        response.status_code = status
        return response

    @app.get("/runbooks", response_class=HTMLResponse)
    async def runbooks_page(request: Request) -> Response:
        principal = await ui(request)
        page = await reviewed(runbooks_of().overview)
        return render_runbooks(principal, "runbooks.html", page=page)

    @app.get("/runbooks/new", response_class=HTMLResponse)
    async def runbook_new(request: Request) -> Response:
        principal = await ui(request)
        service = runbooks_of()
        return render_runbooks(
            principal,
            "runbook_new.html",
            known_targets=service.known_targets(),
            fields={},
        )

    @app.post("/runbooks")
    async def runbook_submit(request: Request) -> Response:
        principal = await ui_mutation(request)
        service = runbooks_of()
        fields = await form_of(request, RUNBOOK_FORM_BYTES)
        try:
            result = await asyncio.to_thread(service.submit, fields, principal.actor_id)
        except ReviewError as exc:
            if not wants_html(request):
                raise _Refusal(
                    _REVIEW_STATUS[exc.error_class], exc.code, **_review_extra(exc)
                ) from None
            return render_runbooks(
                principal,
                "runbook_new.html",
                error=exc,
                status=_REVIEW_STATUS[exc.error_class],
                known_targets=service.known_targets(),
                fields=fields,
            )
        if wants_html(request):
            return RedirectResponse(
                f"/runbooks/submissions/{result['submission_id']}", status_code=303
            )
        return JSONResponse(result)

    @app.get("/runbooks/submissions/{submission_id}", response_class=HTMLResponse)
    async def runbook_submission_page(request: Request, submission_id: str) -> Response:
        principal = await ui(request)
        page = await reviewed(runbooks_of().submission, _uuid_path(submission_id))
        return render_runbooks(principal, "runbook_submission.html", page=page)

    @app.post("/runbooks/submissions/{submission_id}/review")
    async def runbook_decide(request: Request, submission_id: str) -> Response:
        principal = await ui_mutation(request)
        service = runbooks_of()
        subject = _uuid_path(submission_id)
        fields = await form_of(request)
        try:
            result = await asyncio.to_thread(
                service.decide, subject, fields, principal.actor_id
            )
        except ReviewError as exc:
            if not wants_html(request):
                raise _Refusal(
                    _REVIEW_STATUS[exc.error_class], exc.code, **_review_extra(exc)
                ) from None
            page = await reviewed(service.submission, subject)
            return render_runbooks(
                principal,
                "runbook_submission.html",
                page=page,
                error=exc,
                status=_REVIEW_STATUS[exc.error_class],
            )
        if wants_html(request):
            return RedirectResponse(f"/runbooks/submissions/{subject}", status_code=303)
        return JSONResponse(result)

    @app.get("/runbooks/{entry_id}", response_class=HTMLResponse)
    async def runbook_entry_page(request: Request, entry_id: str) -> Response:
        principal = await ui(request)
        page = await reviewed(runbooks_of().entry, _uuid_path(entry_id))
        return render_runbooks(principal, "runbook_entry.html", page=page)

    @app.post("/runbooks/{entry_id}/revoke")
    async def runbook_revoke(request: Request, entry_id: str) -> Response:
        principal = await ui_mutation(request)
        service = runbooks_of()
        subject = _uuid_path(entry_id)
        fields = await form_of(request)
        try:
            result = await asyncio.to_thread(
                service.revoke, subject, fields, principal.actor_id
            )
        except ReviewError as exc:
            if not wants_html(request):
                raise _Refusal(
                    _REVIEW_STATUS[exc.error_class], exc.code, **_review_extra(exc)
                ) from None
            page = await reviewed(service.entry, subject)
            return render_runbooks(
                principal,
                "runbook_entry.html",
                page=page,
                error=exc,
                status=_REVIEW_STATUS[exc.error_class],
            )
        if wants_html(request):
            return RedirectResponse(f"/runbooks/{subject}", status_code=303)
        return JSONResponse(result)

    # -- intake ---------------------------------------------------------

    @app.post("/intake/ui")
    async def intake_ui(request: Request) -> Response:
        principal = await ui_mutation(request)
        fields = await form_of(request)
        envelope = await envelope_from(principal, fields)
        result = await in_thread(workbench.submit, envelope)
        location = f"/incidents/{result.incident_id}"
        if wants_html(request):
            return RedirectResponse(location, status_code=303)
        return JSONResponse(
            {
                "incident_id": str(result.incident_id),
                "run_id": str(result.run_id),
                "replayed": result.replayed,
                "sequence": result.sequence,
            },
            status_code=200 if result.replayed else 201,
            headers={"Location": location},
        )

    @app.post("/intake/events")
    async def intake_event(request: Request) -> Response:
        principal = await generic_event_channel(request)
        payload = await json_of(request)
        try:
            state_time = payload.get("state_time")
            key = delivery_key(
                source=_text(payload, "source"),
                external_event_id=payload.get("external_event_id"),
                source_instance=payload.get("source_instance"),
                object_identity=payload.get("object_identity"),
                state_time=(
                    datetime.fromisoformat(state_time)
                    if isinstance(state_time, str)
                    else None
                ),
                payload_hash=payload.get("payload_hash"),
            )
        except (DomainError, ValueError, TypeError):
            raise _Refusal(400, "INVALID_DELIVERY_KEY") from None
        envelope = await envelope_from(
            principal,
            {
                "target_id": payload.get("target_id"),
                "question": payload.get("question"),
                "idempotency_key": key,
            },
        )
        result = await in_thread(workbench.submit, envelope)
        return JSONResponse(
            {
                "incident_id": str(result.incident_id),
                "run_id": str(result.run_id),
                "replayed": result.replayed,
                "delivery_key": key,
            },
            status_code=200 if result.replayed else 201,
        )

    @app.post("/intake/alertmanager")
    async def intake_alertmanager(request: Request) -> Response:
        """Alertmanager webhook v4 (M1-04 step 2, contract r3 I1-I2).

        Each alert is its own transaction, in payload order; the answer
        lists every alert's outcome. Any ``failed`` alert makes it a 503 so
        Alertmanager retries the group (it retries 5xx only); the alerts
        already committed then come back as replays.
        """
        principal = await generic_event_channel(request)
        body = await body_of(request, ALERTMANAGER_MAX_BODY_BYTES)
        try:
            alerts = parse_payload(json.loads(body.decode("utf-8")))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return JSONResponse(
                {"error": "INVALID_ALERTMANAGER_PAYLOAD"}, status_code=400
            )
        received = await in_thread(clock.now)
        results = []
        for item in alerts:
            result = await in_thread(workbench.intake_alert, principal, item, received)
            results.append(result)
        failed = any(result.outcome == "failed" for result in results)
        return JSONResponse(
            {"results": [result.as_json() for result in results]},
            status_code=503 if failed else 200,
        )

    release_stores: list[ReleaseStore] = [] if releases is None else [releases]

    def release_store() -> ReleaseStore:
        # Without an explicit store, share the workbench's database and pool
        # settings, as the incident adapter does for its ObservationStore.
        if not release_stores:
            durable = getattr(workbench.incidents, "_store", None)
            dsn = getattr(durable, "dsn", None)
            if not isinstance(dsn, str):
                raise _Refusal(503, "STORAGE_UNAVAILABLE")
            release_stores.append(
                ReleaseStore(dsn, pool=getattr(durable, "pool_config", None))
            )
        return release_stores[0]

    @app.post("/intake/version-changes")
    async def intake_release(request: Request) -> Response:
        """Register one release event (F11 PR-3); never authorizes it.

        Only the dedicated ``release-deployer`` actor may submit (U4). A
        payload or target refusal writes nothing and queries nothing (U6);
        a store-level rejection (identity conflict, future time, registry
        mismatch) has its delivery audited by the store and is answered
        with its code. A storage failure is 503 and safe to retry: the
        ``(source, source_release_id)`` key turns a retry into a replay.
        """
        principal = await event_channel(request)
        if principal.actor_id != RELEASE_ACTOR:
            raise _Refusal(403, "RELEASE_ACTOR_REQUIRED")
        body = await body_of(request, RELEASE_MAX_BODY_BYTES)
        profile = workbench.health_profile
        try:
            parsed = parse_release(body)
            resolved = resolve_target(parsed.target, workbench.targets)
            if profile is None:
                raise ReleaseIntakeError(409, "HEALTH_PROFILE_REQUIRED")
            if resolved.health_profile_id != profile.profile_id:
                raise ReleaseIntakeError(422, "HEALTH_PROFILE_TARGET_MISMATCH")
        except ReleaseIntakeError as exc:
            raise _Refusal(exc.status, exc.code) from None
        store = release_store()

        def register() -> Any:
            # The store registers a new target in the release's transaction,
            # so a refused or failed delivery leaves no target row (r4).
            return store.record_release(
                event=parsed.event,
                resource_uid=resolved.resource_uid,
                identity=resolved.identity,
                health_profile_revision=profile.revision,
                health_profile=canonical_content(profile),
                min_tracking_seconds=RELEASE_MIN_TRACKING_SECONDS,
                actor=principal.actor_id,
                raw_body=body,
            )

        try:
            receipt = await asyncio.to_thread(register)
        except PersistenceError:
            raise _Refusal(503, "STORAGE_UNAVAILABLE") from None
        if receipt.outcome == "rejected":
            code = receipt.reason or "RELEASE_REJECTED"
            raise _Refusal(REJECTION_STATUS.get(code, 422), code)
        return JSONResponse(
            {
                "outcome": receipt.outcome,
                "observation_id": str(receipt.observation_id),
                "status": receipt.status,
                "reason": receipt.reason,
            },
            status_code=201 if receipt.outcome == "created" else 200,
        )

    # -- release observation pages (F11 PR-5a) ---------------------------
    # Paths avoid the F7 mutation words ("release", "target", ...) and sit
    # in the intake route's ``version-changes`` family (D1). Basic Auth only:
    # an event token, the release deployer's included, opens none of them.

    def release_of(raw: str) -> UUID:
        try:
            return UUID(raw)
        except ValueError:
            raise _Refusal(404, "UNKNOWN_RELEASE") from None

    @app.get("/version-changes")
    async def release_list(request: Request) -> Response:
        principal = await ui(request)
        releases = await in_thread(workbench.list_releases)
        if wants_html(request):
            return render(
                "version_changes.html", principal=principal, releases=releases
            )
        return JSONResponse({"releases": releases})

    @app.get("/version-changes/{observation_id}")
    async def release_detail(request: Request, observation_id: str) -> Response:
        principal = await ui(request)
        subject = release_of(observation_id)
        page = await in_thread(workbench.release_page, subject)
        if wants_html(request):
            return render(
                "version_change.html",
                principal=principal,
                page=page,
                form_nonce=uuid4().hex,
            )
        return JSONResponse(page)

    @app.post("/version-changes/{observation_id}/control")
    async def release_control(request: Request, observation_id: str) -> Response:
        """Pause, resume or cancel one release observation (C3 section 9)."""
        principal = await ui_mutation(request)
        subject = release_of(observation_id)
        fields = await form_of(request)
        generation = fields.get("expected_control_generation", "")
        action = fields.get("action", "")
        if (
            not generation.isascii()
            or not generation.isdigit()
            or len(generation) > 19
            or action not in ("pause", "resume", "cancel")
        ):
            raise _Refusal(400, "INVALID_INPUT")
        try:
            result = await asyncio.to_thread(
                workbench.release_control,
                subject,
                action=action,
                actor_id=principal.actor_id,
                expected_control_generation=int(generation),
                idempotency_key=fields.get("idempotency_key", ""),
            )
        except WorkbenchError as exc:
            extra = {}
            if exc.current_generation is not None:
                extra["current_generation"] = exc.current_generation
            raise _Refusal(_release_status(exc.code, 409), exc.code, **extra) from None
        except PersistenceError as exc:
            code = str(exc)
            raise _Refusal(_release_status(code, 503), code) from None
        if wants_html(request):
            return RedirectResponse(f"/version-changes/{subject}", status_code=303)
        return JSONResponse(result)

    # -- human control --------------------------------------------------

    @app.post("/incidents/{incident_id}/control")
    async def control(request: Request, incident_id: str) -> Response:
        principal = await ui_mutation(request)
        subject = incident_of(incident_id)
        fields = await form_of(request)
        generation = fields.get("expected_generation", "")
        action = fields.get("action", "")
        if action in MESSAGE_ACTIONS and generation == "":
            # A message is not fenced on the generation (contract B1).
            generation = "0"
        if not generation.isdigit():
            raise _Refusal(400, "INVALID_INPUT")
        text = fields.get("text")
        revision = fields.get("revision")
        to_target = fields.get("to_target")
        into_incident_id = fields.get("into_incident_id")
        raw_retry = fields.get("retry_of")
        if raw_retry is not None and not raw_retry.isdigit():
            raise _Refusal(400, "INVALID_INPUT")
        result = await in_thread(
            workbench.control,
            subject,
            actor_id=principal.actor_id,
            action=action,
            expected_generation=int(generation),
            idempotency_key=fields.get("idempotency_key", ""),
            text=text if text else None,
            revision=revision if revision else None,
            to_target=to_target if to_target else None,
            into_incident_id=into_incident_id if into_incident_id else None,
            retry_of=None if raw_retry is None else int(raw_retry),
        )
        if wants_html(request):
            return RedirectResponse(f"/incidents/{subject}", status_code=303)
        return JSONResponse(
            {
                "incident_id": str(subject),
                "action": result.action,
                "generation": result.generation,
                "replayed": result.replayed,
                "sequence": result.sequence,
                **({"run_id": result.run_id} if result.run_id is not None else {}),
                **(
                    {"input_sequence": result.input_sequence}
                    if result.input_sequence is not None
                    else {}
                ),
            }
        )

    # -- feedback (workbench P5, contract r12) ----------------------------
    # UI identity and Origin, as a control; it is not one (F3).

    @app.post("/incidents/{incident_id}/feedback")
    async def feedback(request: Request, incident_id: str) -> Response:
        principal = await ui_mutation(request)
        subject = incident_of(incident_id)
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        fields: Mapping[str, Any] = (
            await json_of(request)
            if content_type == "application/json"
            else await form_of(request)
        )
        try:
            result = await in_thread(
                workbench.submit_feedback,
                subject,
                actor_id=principal.actor_id,
                fields=fields,
            )
        except _Refusal as exc:
            if exc.code != "REPORT_CHANGED" or not wants_html(request):
                raise
            # F9 (r12): the page again, with the new report, the reason and
            # what the person wrote, still unsaved.
            snapshot = await in_thread(workbench.snapshot, subject)
            context = await incident_context(principal, subject, snapshot)
            response = render(
                "incident.html",
                **context,
                feedback_error=(
                    "The report changed while you were writing — review it "
                    "and submit again."
                ),
                feedback_draft=_feedback_draft(fields),
            )
            response.status_code = 409
            return response
        if wants_html(request):
            return RedirectResponse(f"/incidents/{subject}#feedback", status_code=303)
        return JSONResponse(result)

    @app.get("/feedback/export")
    async def feedback_export(request: Request) -> Response:
        """F13: JSON lines for evaluation, to the signed-in user only."""
        await ui(request)
        rows = await in_thread(workbench.feedback_export)
        body = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        )
        response = Response(body, media_type="application/x-ndjson")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Disposition"] = (
            'attachment; filename="opspilot-feedback.jsonl"'
        )
        return response

    # -- Chat panel (workbench P7-14) -------------------------------------
    # UI identity only (session cookie or Basic), Origin on every POST; the
    # bearer and event tokens are refused by ``ui``.

    def _chat_id(raw: str, code: str) -> UUID:
        try:
            return UUID(raw)
        except ValueError:
            raise _Refusal(404, code) from None

    @app.post("/incidents/{incident_id}/chat")
    async def chat_ask(request: Request, incident_id: str) -> Response:
        principal = await ui_mutation(request)
        subject = incident_of(incident_id)
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        fields: Mapping[str, Any] = (
            await json_of(request)
            if content_type == "application/json"
            else await form_of(request)
        )
        question = fields.get("question", fields.get("text"))
        key = fields.get("idempotency_key")
        raw_session = fields.get("session_id") or None
        if not isinstance(question, str) or not isinstance(key, str):
            raise _Refusal(400, "INVALID_INPUT")
        if raw_session is not None and not isinstance(raw_session, str):
            raise _Refusal(400, "INVALID_INPUT")
        session_id = (
            None
            if raw_session is None
            else _chat_id(raw_session, "CHAT_UNKNOWN_SESSION")
        )
        result = await in_thread(
            workbench.chat_ask,
            subject,
            actor_id=principal.actor_id,
            question=question,
            idempotency_key=key,
            session_id=session_id,
        )
        return JSONResponse(result, status_code=200 if result["replayed"] else 201)

    @app.get("/incidents/{incident_id}/chat/sessions")
    async def chat_sessions(request: Request, incident_id: str) -> Response:
        await ui(request)
        subject = incident_of(incident_id)
        sessions = await in_thread(workbench.chat_sessions, subject)
        response = JSONResponse({"incident_id": str(subject), "sessions": sessions})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/incidents/{incident_id}/chat/sessions/{session_id}")
    async def chat_session(
        request: Request, incident_id: str, session_id: str
    ) -> Response:
        await ui(request)
        subject = incident_of(incident_id)
        found = await in_thread(
            workbench.chat_session,
            subject,
            _chat_id(session_id, "CHAT_UNKNOWN_SESSION"),
        )
        response = JSONResponse(found)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/incidents/{incident_id}/chat/turns/{turn_id}/forward")
    async def chat_forward(
        request: Request, incident_id: str, turn_id: str
    ) -> Response:
        await ui_mutation(request)
        subject = incident_of(incident_id)
        result = await in_thread(
            workbench.chat_forward, subject, _chat_id(turn_id, "CHAT_UNKNOWN_TURN")
        )
        return JSONResponse(result)

    # -- progress stream ------------------------------------------------

    @app.get("/incidents/{incident_id}/events")
    async def events(request: Request, incident_id: str) -> Response:
        await ui(request)
        subject = incident_of(incident_id)
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor is None:
            raw_cursor = request.query_params.get("cursor", "0")
        if not raw_cursor.isdigit():
            raise _Refusal(400, "INVALID_CURSOR")
        if await in_thread(workbench.incidents.find_incident, subject) is None:
            raise _Refusal(404, "UNKNOWN_INCIDENT")
        # A completion whose event was lost is announced before the stream
        # replays, so a reconnecting page sees it; the stream repeats the
        # repair while it follows, for a loss that happens after this point.
        await in_thread(workbench.reconcile, subject)
        return StreamingResponse(
            _stream(
                workbench,
                subject,
                int(raw_cursor),
                sse_poll_seconds,
                sse_idle_seconds,
                sse_repair_seconds,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # -- evidence read-back ---------------------------------------------

    @app.get("/incidents/{incident_id}/evidence/{evidence_id}")
    async def evidence(
        request: Request, incident_id: str, evidence_id: str
    ) -> Response:
        principal = await ui(request)
        subject = incident_of(incident_id)
        record = await in_thread(workbench.evidence_for, subject, evidence_id)
        if record is None:
            # A postmortem may cite a recovery reading; its link resolves to
            # the stored observation row, bound to the same incident.
            found = await in_thread(
                workbench.recovery_reading_for, subject, evidence_id
            )
            if found is None:
                raise _Refusal(404, "UNKNOWN_EVIDENCE")
            return JSONResponse(_recovery_reading(evidence_id, found))
        # A cleaned record (#244) keeps its identity, hashes and view; the
        # two cleaned keys appear only then, so a present record keeps the
        # page's established key set.
        # Only the state says cleaned; a record without raw in any other
        # state is unverifiable (``hashes_verified`` None) and shows no raw.
        raw, raw_redacted = (
            (_CLEANED, False)
            if record.payload_state == "cleaned"
            else ({}, False)
            if record.raw is None
            else _raw_fields(record.raw)
        )
        stored_view = dict(record.view)
        view = redact_payload(stored_view)
        payload = {
            "evidence_id": record.evidence_id,
            "run_id": record.run_id,
            "status": record.status,
            "adopted": record.adopted,
            "raw_sha256": record.raw_sha256,
            "view_sha256": record.view_sha256,
            "hashes_verified": record.hashes_verified,
            "projection_revision": record.projection_revision,
            "observed_at": record.observed_at.isoformat(),
            "data_as_of": (
                None if record.data_as_of is None else record.data_as_of.isoformat()
            ),
            "view": view,
            **raw,
        }
        if raw_redacted or view is not stored_view:
            # The hashes and ``hashes_verified`` stay over the stored bytes;
            # this says what is shown was redacted for display and no longer
            # hashes to them (evidence or readings stored before F7 U7-a,
            # never rewritten). Absent otherwise, so evidence redacted at
            # persistence keeps the page's established key set.
            payload["display_redacted"] = True
        if wants_html(request):
            return render(
                "evidence.html",
                evidence=payload,
                incident_id=incident_id,
                principal=principal,
            )
        return JSONResponse(payload)

    return app


#: What a cleaned payload shows in place of its raw fields (#244 PR-3).
_CLEANED = {"payload_state": "cleaned", "raw_unavailable": "CLEANED_DATA"}


def _raw_fields(raw: bytes) -> tuple[dict[str, str], bool]:
    """The raw bytes as shown, and whether display redaction changed them."""
    shown = redact_json_bytes(raw)
    try:
        return {"raw_utf8": shown.decode("utf-8")}, shown is not raw
    except UnicodeDecodeError:
        return {"raw_base64": base64.b64encode(raw).decode("ascii")}, False


def _bundle_bodies_redacted(raw: bytes) -> bytes:
    """An Observer reading bundle with each base64 source body redacted.

    The bundle (``observer.sampler._bundle``) stores every response body as
    ``body_b64``; redacting the outer JSON only sees base64 text, so each
    body is decoded, redacted and re-encoded for display (bot review). The
    stored bytes and their ``body_sha256`` are untouched. Anything that is
    not such a bundle comes back as the same object.
    """
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return raw
    if not isinstance(document, dict):
        return raw
    changed = False
    for part in document.values():
        if not isinstance(part, dict) or not isinstance(part.get("body_b64"), str):
            continue
        try:
            body = base64.b64decode(part["body_b64"], validate=True)
        except ValueError:
            continue
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            # Bytes that are not text cannot be examined for credentials;
            # base64 is no redaction, so the body is not shown at all.
            part["body_b64"] = ""
            part["body_omitted_for_display"] = True
            changed = True
            continue
        shown = redact_json_bytes(body)
        if shown is not body:
            part["body_b64"] = base64.b64encode(shown).decode("ascii")
            changed = True
    if not changed:
        return raw
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _recovery_reading(evidence_id: str, found: Mapping[str, Any]) -> dict[str, Any]:
    """One stored signal reading of an observation sample, as captured."""
    sample, reading = found["sample"], found["reading"]
    raw = reading.get("raw")
    raw_sha256 = reading.get("raw_sha256")
    raw_fields: dict[str, str] = {}
    raw_redacted = False
    if raw is not None:
        stored = bytes(raw)
        shown = _bundle_bodies_redacted(stored)
        raw_fields, raw_redacted = _raw_fields(shown)
        raw_redacted = raw_redacted or shown is not stored
    value = redact_payload(reading["value"])
    query = redact_payload(reading["query"])
    return {
        "evidence_id": evidence_id,
        "kind": "recovery",
        "session_id": str(found["session_id"]),
        "target": dict(found["target"]),
        "sample_id": str(sample["sample_id"]),
        "sample_sequence": int(sample["sequence"]),
        "health_profile_revision": sample.get("health_profile_revision"),
        "submitted_at": (
            None
            if sample.get("submitted_at") is None
            else sample["submitted_at"].isoformat()
        ),
        "disposition": str(sample["disposition"]),
        "readings_consistent": bool(sample["readings_consistent"]),
        "signal_name": reading["signal_name"],
        "status": reading["status"],
        "value": value,
        "sample_count": reading["sample_count"],
        "query": query,
        "window_start": reading["window_start"].isoformat(),
        "window_end": reading["window_end"].isoformat(),
        "source": reading["source"],
        "raw_sha256": raw_sha256,
        # None: no raw bundle was stored for this reading; a cleaned one
        # (#244) must still keep a digest, else it is not verified
        "hashes_verified": (
            (
                False
                if reading.get("payload_state") == "cleaned"
                and re.fullmatch(r"[0-9a-f]{64}", raw_sha256 or "") is None
                else None
            )
            if raw is None
            else hashlib.sha256(bytes(raw)).hexdigest() == raw_sha256
        ),
        **raw_fields,
        **(_CLEANED if reading.get("payload_state") == "cleaned" else {}),
        **(
            {"display_redacted": True}
            if raw_redacted
            or value is not reading["value"]
            or query is not reading["query"]
            else {}
        ),
    }


def safe_next(raw: str | None) -> str:
    """Where the login form sends the browser back: a path on this site.

    Anything else (another host, ``//host``, ``/\\host``, a control
    character a browser would strip into one of those) is ``/``.
    """
    if (
        not raw
        or len(raw) > 2048
        or not raw.startswith("/")
        or raw.startswith(("//", "/\\"))
        or "\\" in raw
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw)
    ):
        return "/"
    return raw


def _review_extra(exc: ReviewError) -> dict[str, Any]:
    extra = exc.payload()
    extra.pop("code")
    return extra


def _feedback_draft(fields: Mapping[str, Any]) -> dict[str, Any]:
    """A refused feedback form's own values, to fill the form again."""

    def text(name: str) -> str | None:
        value = fields.get(name)
        return value if isinstance(value, str) and value.strip() else None

    steering = {"yes": True, "true": True, "no": False, "false": False}.get(
        str(fields.get("steering_needed", "")).strip().lower()
    )
    return {
        "verdict": text("verdict"),
        "actual_root_cause": text("actual_root_cause"),
        "steering_needed": steering,
        "next_steps_verdict": text("next_steps_verdict"),
        "correct_next_steps": text("correct_next_steps"),
        "notes": text("notes"),
    }


def _invalid(name: str) -> _Refusal:
    """A malformed review form field, in the shape of ``ReviewError`` (R3)."""
    return _Refusal(400, "INVALID_INPUT", error_class="invalid", fields=[name])


def _uuid_path(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise _Refusal(404, "NOT_FOUND") from None


def _postmortem_path(raw_id: str, raw_version: str) -> tuple[UUID, int]:
    if not raw_version.isdigit() or int(raw_version) < 1:
        raise _Refusal(404, "NOT_FOUND")
    return _uuid_path(raw_id), int(raw_version)


def _generation(value: str) -> int:
    if not value.isdigit():
        raise ValueError(value)
    return int(value)


def _expected(fields: Mapping[str, str]) -> int:
    raw = fields.get("expected_generation", "")
    if not raw.isdigit():
        raise _invalid("expected_generation")
    return int(raw)


def _text(fields: Mapping[str, Any], name: str) -> str:
    """A required string field; anything else is a 400 with no echo."""
    value = fields.get(name)
    if not isinstance(value, str):
        raise _Refusal(400, "INVALID_INPUT")
    return value


def _sse(kind: str, payload: Mapping[str, Any], sequence: int | None = None) -> bytes:
    lines = []
    if sequence is not None:
        lines.append(f"id: {sequence}")
    lines.append(f"event: {kind}")
    data = json.dumps(payload, ensure_ascii=False, default=str)
    lines.extend(f"data: {line}" for line in data.splitlines() or [""])
    return ("\n".join(lines) + "\n\n").encode("utf-8")


async def _stream(
    workbench: Workbench,
    subject: UUID,
    cursor: int,
    poll_seconds: float,
    idle_seconds: float | None,
    repair_seconds: float,
) -> AsyncIterator[bytes]:
    """Replay from ``cursor`` then follow. Ends only on reload, idle or disconnect.

    A browser drop does not touch the Run: nothing here holds a lease or a
    transaction. A cursor older than the retained floor gets ``reload`` so
    the page fetches the snapshot instead of trusting a gapped stream.
    While following, every ``repair_seconds`` of silence the stream runs
    the same idempotent ``reconcile`` the endpoint ran before replaying:
    a worker that published and died before announcing after the stream
    opened would otherwise leave a long-lived stream on a stale page.
    """
    yield b"retry: 2000\n\n"
    idle = 0.0
    quiet = 0.0
    while True:
        try:
            batch = await asyncio.to_thread(
                workbench.events.read_after, subject, cursor, limit=100
            )
        except CursorTooOld:
            yield _sse("reload", {"reason": "CURSOR_TOO_OLD", "cursor": cursor})
            return
        if batch:
            for event in batch:
                yield _sse(event.kind, dict(event.payload), event.sequence)
                cursor = event.sequence
            idle = 0.0
            quiet = 0.0
            continue
        if quiet >= repair_seconds:
            quiet = 0.0
            await asyncio.to_thread(workbench.reconcile, subject)
            continue
        if idle_seconds is not None and idle >= idle_seconds:
            yield b": idle\n\n"
            return
        await asyncio.sleep(poll_seconds)
        idle += poll_seconds
        quiet += poll_seconds
