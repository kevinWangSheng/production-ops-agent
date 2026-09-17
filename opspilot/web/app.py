"""FastAPI/Jinja workbench with the two authenticated intake channels.

Routes (C3 section 9): incident list and detail, the SSE progress stream
with cursor resume, the UI intake form, the token-authenticated event
intake, a human-control endpoint and evidence read-back. Every handler
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
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from opspilot.domain.base import DomainError
from opspilot.domain.intake import delivery_key
from opspilot.intake import IntakeEnvelope, IntakeRequest, Principal, verify_channel
from opspilot.persistence import PersistenceError
from opspilot.tools.executor import Clock
from opspilot.web.auth import Authenticator, AuthError
from opspilot.web.events import CursorTooOld
from opspilot.web.service import CONTROL_ACTIONS, Workbench, WorkbenchError

MAX_BODY_BYTES = 64 * 1024
_TEMPLATES = Path(__file__).with_name("templates")
_FORM = "application/x-www-form-urlencoded"

_AUTH_STATUS = {
    "MISSING_CREDENTIALS": 401,
    "INVALID_CREDENTIALS": 401,
    "ORIGIN_REJECTED": 403,
}
_WORKBENCH_STATUS = {
    "UNKNOWN_INCIDENT": 404,
    "INVALID_INPUT": 400,
    "INVALID_ACTION": 400,
    "TEXT_REQUIRED": 400,
    "TEXT_NOT_ALLOWED": 400,
    "INTAKE_KEY_CONFLICT": 409,
    "CONTROL_KEY_CONFLICT": 409,
    "CONTROL_CONFLICT": 409,
    "ILLEGAL_TRANSITION": 409,
    "IDENTITY_CONFLICT": 409,
    "UNKNOWN_IDENTITY": 404,
    "STORAGE_UNAVAILABLE": 503,
    "RETRY": 503,
    "TIMEOUT": 503,
}


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
) -> FastAPI:
    app = FastAPI(title="OpsPilot workbench", docs_url=None, redoc_url=None)
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )

    def render(name: str, **context: Any) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(**context))

    @app.exception_handler(_Refusal)
    async def refused(request: Request, exc: _Refusal) -> Response:
        headers = {}
        if exc.status == 401 and not request.url.path.startswith("/intake/events"):
            headers["WWW-Authenticate"] = 'Basic realm="opspilot"'
        return JSONResponse(
            {"code": exc.code, **exc.extra}, status_code=exc.status, headers=headers
        )

    async def ui(request: Request) -> Principal:
        try:
            principal = authenticator.ui_principal(request.headers)
            return verify_channel(principal, expected="ui_basic")
        except AuthError as exc:
            raise _Refusal(_AUTH_STATUS.get(exc.code, 401), exc.code) from None
        except DomainError:
            raise _Refusal(403, "CHANNEL_MISMATCH") from None

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

    async def body_of(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > MAX_BODY_BYTES
        ):
            raise _Refusal(413, "BODY_TOO_LARGE")
        # Stream so an undeclared or chunked body is capped, not buffered.
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise _Refusal(413, "BODY_TOO_LARGE")
            chunks.append(chunk)
        return b"".join(chunks)

    async def form_of(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        if content_type != _FORM:
            raise _Refusal(415, "FORM_REQUIRED")
        body = await body_of(request)
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

    # -- pages ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        principal = await ui(request)
        incidents = await in_thread(workbench.list_incidents)
        return render("index.html", principal=principal, incidents=incidents)

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    async def incident_page(request: Request, incident_id: str) -> Response:
        principal = await ui(request)
        snapshot = await in_thread(workbench.snapshot, incident_of(incident_id))
        return render(
            "incident.html",
            principal=principal,
            snapshot=snapshot,
            actions=sorted(CONTROL_ACTIONS),
            # One fresh key per rendered form: two operators on the same page
            # state must not share an idempotency key.
            form_nonce=uuid4().hex,
        )

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
        principal = await event_channel(request)
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

    # -- human control --------------------------------------------------

    @app.post("/incidents/{incident_id}/control")
    async def control(request: Request, incident_id: str) -> Response:
        principal = await ui_mutation(request)
        subject = incident_of(incident_id)
        fields = await form_of(request)
        generation = fields.get("expected_generation", "")
        if not generation.isdigit():
            raise _Refusal(400, "INVALID_INPUT")
        text = fields.get("text")
        result = await in_thread(
            workbench.control,
            subject,
            actor_id=principal.actor_id,
            action=fields.get("action", ""),
            expected_generation=int(generation),
            idempotency_key=fields.get("idempotency_key", ""),
            text=text if text else None,
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
            }
        )

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
        return StreamingResponse(
            _stream(
                workbench, subject, int(raw_cursor), sse_poll_seconds, sse_idle_seconds
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # -- evidence read-back ---------------------------------------------

    @app.get("/incidents/{incident_id}/evidence/{evidence_id}")
    async def evidence(
        request: Request, incident_id: str, evidence_id: str
    ) -> Response:
        await ui(request)
        subject = incident_of(incident_id)
        record = await in_thread(workbench.evidence_for, subject, evidence_id)
        if record is None:
            raise _Refusal(404, "UNKNOWN_EVIDENCE")
        try:
            raw: dict[str, Any] = {"raw_utf8": record.raw.decode("utf-8")}
        except UnicodeDecodeError:
            raw = {"raw_base64": base64.b64encode(record.raw).decode("ascii")}
        return JSONResponse(
            {
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
                "view": dict(record.view),
                **raw,
            }
        )

    return app


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
) -> AsyncIterator[bytes]:
    """Replay from ``cursor`` then follow. Ends only on reload, idle or disconnect.

    A browser drop does not touch the Run: nothing here holds a lease or a
    transaction. A cursor older than the retained floor gets ``reload`` so
    the page fetches the snapshot instead of trusting a gapped stream.
    """
    yield b"retry: 2000\n\n"
    idle = 0.0
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
            continue
        if idle_seconds is not None and idle >= idle_seconds:
            yield b": idle\n\n"
            return
        await asyncio.sleep(poll_seconds)
        idle += poll_seconds
