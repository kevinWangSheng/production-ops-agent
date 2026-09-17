"""Thin DeepSeek Chat Completions client for the investigation loop.

Credentials travel only on the Authorization header and never enter
``ModelCall`` or ``ModelReply``. 400/422 are not retried. 429 and network
errors surface as ``MODEL_UNAVAILABLE`` so the loop can count, re-reserve
and re-check the deadline before any retry.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from opspilot.investigation.limits import MAX_HTTP_RESPONSE_BYTES
from opspilot.investigation.loop import (
    ACCEPTED_RESPONSE_MODEL,
    ModelCall,
    ModelError,
    ModelReply,
    serialized_request,
)

_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
_RETRYABLE_STATUS = frozenset({429, 500, 503})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _Opener(Protocol):
    """The one method this client uses from ``urllib``'s ``OpenerDirector``."""

    def open(self, request: Request, timeout: float) -> Any: ...


class MonotonicClock(Protocol):
    """Wall-clock elapsed time only -- never wall-clock-of-day.

    Deliberately narrower than ``opspilot.tools.executor.Clock``: this
    client never makes a lease/deadline decision (that is the loop's job,
    against ``request.scope.deadline``), so it has no business reading
    calendar time at all, and ``opspilot``'s product code is not allowed to
    call ``datetime.now()`` outside the database clock
    (``DurableStore._db_now``) regardless. Any object with ``.monotonic()``
    -- including a real ``Clock`` or a ``FakeClock`` -- already satisfies
    this.
    """

    def monotonic(self) -> float: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


class DeepSeekClient:
    """Stdlib HTTPS client. One complete response per ``complete`` call."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = _ENDPOINT,
        model: str = ACCEPTED_RESPONSE_MODEL,
        clock: MonotonicClock | None = None,
        opener: _Opener | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ModelError("MODEL_REJECTED")
        if not endpoint.startswith("https://"):
            raise ModelError("MODEL_REJECTED")
        self._api_key = api_key
        self._endpoint = endpoint
        self._model = model
        self._clock = clock if clock is not None else _SystemClock()
        self._opener = (
            opener
            if opener is not None
            else build_opener(ProxyHandler({}), _NoRedirect())
        )

    def complete(self, call: ModelCall) -> ModelReply:
        """One physical HTTPS request. Retries are the loop's budget decision."""
        body = serialized_request(call)
        raw, status = self._post(body, call.timeout_seconds)
        if status in {400, 401, 402, 422}:
            raise ModelError("MODEL_REJECTED")
        if status in _RETRYABLE_STATUS or status < 200 or status >= 300:
            raise ModelError("MODEL_UNAVAILABLE")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ModelError("MODEL_UNAVAILABLE") from exc
        return _parse_reply(payload)

    def _post(self, body: bytes, timeout: float) -> tuple[bytes, int]:
        request = Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
            },
        )
        # ``timeout`` here already is ``min(360s frozen limit, remaining
        # deadline, remaining wall)`` (the loop's ``_remaining_timeout``).
        # ``urlopen``'s ``timeout`` only bounds each individual socket
        # operation, not the whole response: a slow trickle -- many small
        # chunks, each arriving just under that per-read timeout -- could
        # keep ``_read_capped`` looping far past this budget. ``deadline``
        # below is what actually enforces the wall-clock total.
        deadline = self._clock.monotonic() + max(timeout, 0.1)
        try:
            with self._opener.open(request, timeout=max(timeout, 0.1)) as response:
                raw = _read_capped(response, self._clock, deadline)
                return raw, int(response.status)
        except HTTPError as exc:
            try:
                exc.read(MAX_HTTP_RESPONSE_BYTES + 1)
            except OSError:
                pass
            return b"", int(exc.code)
        except (URLError, TimeoutError, OSError) as exc:
            raise ModelError("MODEL_UNAVAILABLE") from exc


def _read_capped(response: Any, clock: MonotonicClock, deadline: float) -> bytes:
    chunks = bytearray()
    while True:
        if clock.monotonic() >= deadline:
            raise ModelError("MODEL_UNAVAILABLE")
        piece = response.read(65536)
        if not piece:
            break
        chunks.extend(piece)
        if len(chunks) > MAX_HTTP_RESPONSE_BYTES:
            raise ModelError("RESPONSE_TOO_LARGE")
    return bytes(chunks)


def _parse_reply(payload: Mapping[str, Any]) -> ModelReply:
    try:
        choices = payload["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ValueError
        message = choice["message"]
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            raise ValueError
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError
        if "tool_calls" not in message or message["tool_calls"] is None:
            calls: list[Any] = []
        else:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or any(
                not isinstance(call, Mapping) for call in calls
            ):
                raise ValueError
        finish = choice.get("finish_reason")
        if not isinstance(finish, str) or not finish:
            raise ValueError
        reported = payload.get("model")
        if not isinstance(reported, str) or not reported:
            raise ValueError
        usage_obj = payload.get("usage")
        usage: dict[str, Any] = dict(usage_obj) if isinstance(usage_obj, dict) else {}
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelError("MODEL_UNAVAILABLE") from exc
    return ModelReply(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tuple(dict(call) for call in calls),
        finish_reason=finish,
        response_model=reported,
        usage=usage,
        raw={"id": response_id, "usage": usage},
    )
