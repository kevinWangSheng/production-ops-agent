"""Thin DeepSeek Chat Completions client for the investigation loop.

Credentials travel only on the Authorization header and never enter
``ModelCall`` or ``ModelReply``. 400/422 are not retried; 429 and network
errors get a single bounded retry (technical plan section 5).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from opspilot.investigation.limits import MAX_HTTP_RESPONSE_BYTES
from opspilot.investigation.loop import (
    ACCEPTED_RESPONSE_MODEL,
    ModelCall,
    ModelError,
    ModelReply,
)

_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
_RETRYABLE_STATUS = frozenset({429, 500, 503})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class DeepSeekClient:
    """Stdlib HTTPS client. One complete response per ``complete`` call."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = _ENDPOINT,
        model: str = ACCEPTED_RESPONSE_MODEL,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ModelError("MODEL_REJECTED")
        if not endpoint.startswith("https://"):
            raise ModelError("MODEL_REJECTED")
        self._api_key = api_key
        self._endpoint = endpoint
        self._model = model
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def complete(self, call: ModelCall) -> ModelReply:
        """One physical HTTPS request. Retries are the loop's budget decision."""
        body = _request_body(call, self._model)
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
        try:
            with self._opener.open(request, timeout=max(timeout, 0.1)) as response:
                raw = _read_capped(response)
                return raw, int(response.status)
        except HTTPError as exc:
            try:
                exc.read(MAX_HTTP_RESPONSE_BYTES + 1)
            except OSError:
                pass
            return b"", int(exc.code)
        except (URLError, TimeoutError, OSError) as exc:
            raise ModelError("MODEL_UNAVAILABLE") from exc


def _request_body(call: ModelCall, model: str) -> bytes:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [dict(message) for message in call.messages],
        "max_tokens": call.max_tokens,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "stream": False,
    }
    if call.json_mode:
        payload["response_format"] = {"type": "json_object"}
    if call.tools is not None:
        payload["tools"] = [dict(tool) for tool in call.tools]
        payload["tool_choice"] = "auto"
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _read_capped(response: Any) -> bytes:
    chunks = bytearray()
    while True:
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
        calls = message.get("tool_calls") or []
        if calls == []:
            calls = []
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
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelError("MODEL_UNAVAILABLE") from exc
    return ModelReply(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tuple(dict(call) for call in calls),
        finish_reason=finish,
        response_model=reported,
        usage=usage,
        raw={"id": payload.get("id"), "usage": usage},
    )
