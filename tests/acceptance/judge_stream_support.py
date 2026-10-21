"""SSE fixtures for the judge streaming contract; written without reading the impl.

Authority: docs/tasks/2026-10-20-judge-streaming.md decision table S1-S15
(cited S<n>) and docs/tasks/2026-10-14-eval-pr-b3-interface.md (B3:<line>).
Every response is served by an in-process ``httpx2.MockTransport``; nothing
here opens a socket or reads ``.env``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx2
from eval_b3_support import MODELS, USAGE, content

from opspilot.evaluation.judge import HTTPJudgeTransport

KIB = 1024
MIB = 1024 * KIB
# A semantically valid judge answer for the default ``case()`` input: a fault
# scenario with zero diagnoses recomputes to False, so verdict False is
# consistent and judge_once reports status "ok" (checked offline by
# test_fixture_answer_is_ok_through_non_streaming_fake).
ANSWER = content((), False)


def chunk(
    *,
    provider: str = "zai",
    delta: Mapping[str, Any] | None = None,
    finish: str | None = None,
    usage: Any = None,
    choices: list[Any] | None = None,
    ident: str = "chatcmpl-stream-1",
    model: Any = ...,
    drop: Iterable[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One chat.completion.chunk object (OpenAI/Z.ai/DeepSeek common shape)."""
    doc: dict[str, Any] = {
        "id": ident,
        "object": "chat.completion.chunk",
        "created": 1_760_000_000,
        "model": MODELS[provider] if model is ... else model,
        "choices": choices
        if choices is not None
        else [{"index": 0, "delta": dict(delta or {}), "finish_reason": finish}],
    }
    if usage is not None:
        doc["usage"] = usage
    doc.update(extra or {})
    for key in drop:
        doc.pop(key, None)
    return doc


def answer_chunks(
    provider: str = "zai",
    *,
    text: str = ANSWER,
    pieces: int = 3,
    finish: str = "stop",
    usage: Any = USAGE,
    openai_usage_tail: bool = False,
) -> list[dict[str, Any]]:
    """Role first, content split in ``pieces``, then the finish chunk.

    ``openai_usage_tail`` gives the OpenAI include_usage shape: finish chunk
    without usage followed by a ``choices: []`` chunk carrying usage (S13).
    Otherwise usage rides on the finish chunk (Z.ai/DeepSeek shape).
    """
    step = max(1, -(-len(text) // pieces))
    parts = [text[i : i + step] for i in range(0, len(text), step)]
    out = [chunk(provider=provider, delta={"role": "assistant", "content": ""})]
    out += [chunk(provider=provider, delta={"content": p}) for p in parts]
    if openai_usage_tail:
        out.append(chunk(provider=provider, finish=finish))
        out.append(chunk(provider=provider, choices=[], usage=usage))
    else:
        out.append(chunk(provider=provider, finish=finish, usage=usage))
    return out


def data_event(obj: Any, *, eol: bytes = b"\n") -> bytes:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return b"data: " + raw + eol + eol


DONE = b"data: [DONE]\n\n"


def sse_body(events: Iterable[Any], *, done: bool = True, eol: bytes = b"\n") -> bytes:
    body = b"".join(data_event(e, eol=eol) for e in events)
    if done:
        body += b"data: [DONE]" + eol + eol
    return body


def split(raw: bytes, size: int) -> list[bytes]:
    return [raw[i : i + size] for i in range(0, len(raw), size)] or [b""]


class Pieces(httpx2.AsyncByteStream):
    """Yields given byte pieces; may raise or stall after them."""

    def __init__(
        self,
        pieces: Iterable[bytes] | Callable[[], Iterable[bytes]],
        *,
        then_raise: BaseException | None = None,
        then_sleep: float = 0.0,
        delay: float = 0.0,
    ) -> None:
        self.pieces = pieces
        self.then_raise = then_raise
        self.then_sleep = then_sleep
        self.delay = delay

    async def __aiter__(self):
        pieces = self.pieces() if callable(self.pieces) else self.pieces
        for piece in pieces:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield piece
        if self.then_sleep:
            await asyncio.sleep(self.then_sleep)
        if self.then_raise is not None:
            raise self.then_raise


class Wire:
    """Serves one canned response and records each request it saw."""

    def __init__(
        self,
        body: bytes | Pieces = b"",
        *,
        status: int = 200,
        content_type: str | None = "text/event-stream",
        piece: int | None = None,
    ) -> None:
        self.requests: list[httpx2.Request] = []
        self.body = body
        self.status = status
        self.content_type = content_type
        self.piece = piece

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        headers = (
            {} if self.content_type is None else {"content-type": self.content_type}
        )
        stream = self.body
        if isinstance(stream, bytes):
            stream = Pieces(split(stream, self.piece or max(1, len(stream))))
        return httpx2.Response(self.status, headers=headers, stream=stream)

    def transport(self) -> HTTPJudgeTransport:
        return HTTPJudgeTransport(transport=httpx2.MockTransport(self))
