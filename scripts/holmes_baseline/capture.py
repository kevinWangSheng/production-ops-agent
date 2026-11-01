"""Offline request check before the live Run (#396 acceptance 3).

A fake Chat Completions endpoint on the internal network: it records the JSON
body Holmes/LiteLLM would send to the model API (the Authorization header is
dropped, the key is a dummy anyway) and answers with a fixed final reply so
``holmes ask`` ends after one call. ``request_check`` then asserts that the
outgoing body carries thinking and reasoning effort as configured.

With ``--script <path>`` (#385, F14 step 3, R7) the endpoint is scripted
instead: the n-th model turn (n = assistant messages already in the request)
answers ``turns[n]`` of the script -- tool calls, or a final reply -- and
every request is printed whole (messages and tools included) as one
``{"kind": "scripted_request", ...}`` line. Those lines only reach
``docker logs`` and the git-ignored raw directory; the evidence keeps hashes
and section snippets. The default mode is unchanged byte for byte.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SCRIPT_SCHEMA = "opspilot.f14.capture-script.v1"
REPLY = "capture endpoint: request recorded"
# What OpsPilot sends (opspilot.investigation.loop.serialized_request); kept as
# literals: this file runs alone inside the helper container.
EXPECTED_MODEL = "deepseek-flash"
EXPECTED_MAX_TOKENS = 65536


def request_check(body: dict[str, Any]) -> dict[str, Any]:
    """Whether the captured body carries OpsPilot's request settings (U6):
    the same model, thinking enabled, effort high and the 65536 output limit.

    LiteLLM may forward ``thinking`` top-level or inside ``extra_body``
    (M0 saw it rejected top-level), so both places count.
    """
    extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    thinking = body.get("thinking", extra.get("thinking"))
    effort = body.get("reasoning_effort", extra.get("reasoning_effort"))
    limit = body.get("max_tokens", body.get("max_completion_tokens"))
    checks = {
        "model_" + EXPECTED_MODEL: body.get("model") == EXPECTED_MODEL,
        "thinking_enabled": thinking == {"type": "enabled"},
        "reasoning_effort_high": effort == "high",
        f"max_tokens_{EXPECTED_MAX_TOKENS}": limit == EXPECTED_MAX_TOKENS,
    }
    return {**checks, "model": body.get("model"), "ok": all(checks.values())}


def _completion(model: str) -> dict[str, Any]:
    return {
        "id": "capture-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": REPLY},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def load_script(path: str) -> list[dict[str, Any]]:
    """The script's turns; each is ``{"tool_calls": [...]}`` or ``{"final": str}``
    and the last one must be a final reply."""
    with open(path) as handle:
        script = json.load(handle)
    turns = script.get("turns") if isinstance(script, dict) else None
    if (
        script.get("schema") != SCRIPT_SCHEMA
        or not isinstance(turns, list)
        or not turns
    ):
        raise SystemExit(f"{path}: not a {SCRIPT_SCHEMA} script")
    for turn in turns:
        calls = turn.get("tool_calls") if isinstance(turn, dict) else None
        final = turn.get("final") if isinstance(turn, dict) else None
        if (calls is None) == (final is None):
            raise SystemExit(f"{path}: a turn needs exactly one of tool_calls/final")
        if calls is not None and not (
            isinstance(calls, list)
            and calls
            and all(
                isinstance(c, dict)
                and isinstance(c.get("name"), str)
                and isinstance(c.get("arguments"), dict)
                for c in calls
            )
        ):
            raise SystemExit(f"{path}: bad tool_calls")
        if final is not None and not isinstance(final, str):
            raise SystemExit(f"{path}: final must be a string")
    if "final" not in turns[-1]:
        raise SystemExit(f"{path}: the last turn must be a final reply")
    return turns


def scripted_turn(turns: list[dict[str, Any]], body: dict[str, Any]) -> int:
    """Which turn answers this request: the number of assistant messages it
    already carries (retries of the same turn get the same answer), capped at
    the final turn."""
    done = sum(
        1
        for m in body.get("messages") or []
        if isinstance(m, dict) and m.get("role") == "assistant"
    )
    return min(done, len(turns) - 1)


def _tool_calls(turn_index: int, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"call_{turn_index}_{i}",
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call["arguments"], sort_keys=True),
            },
        }
        for i, call in enumerate(calls)
    ]


def scripted_message(
    turn_index: int, turn: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """The assistant message and finish reason for one scripted turn."""
    if "tool_calls" in turn:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": _tool_calls(turn_index, turn["tool_calls"]),
        }
        return message, "tool_calls"
    return {"role": "assistant", "content": turn["final"]}, "stop"


def scripted_payload(
    turn_index: int, turn: dict[str, Any], model: str, stream: bool
) -> tuple[bytes, str]:
    message, finish = scripted_message(turn_index, turn)
    ident = f"scripted-{turn_index}"
    if not stream:
        completion = {
            "id": ident,
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return json.dumps(completion).encode(), "application/json"
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("tool_calls"):
        delta["tool_calls"] = [
            {"index": i, **call} for i, call in enumerate(message["tool_calls"])
        ]
    else:
        delta["content"] = message["content"]
    chunks = [
        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]
    body = "".join(
        "data: "
        + json.dumps(
            {
                "id": ident,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                **chunk,
            }
        )
        + "\n\n"
        for chunk in chunks
    )
    return (body + "data: [DONE]\n\n").encode(), "text/event-stream"


SCRIPT: list[dict[str, Any]] | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        if SCRIPT is not None:
            self._scripted(body)
            return
        print(
            json.dumps(
                {
                    "path": self.path,
                    "header_names": sorted(
                        h.lower() for h in self.headers if h.lower() != "authorization"
                    ),
                    "body": {
                        k: v for k, v in body.items() if k not in ("messages", "tools")
                    },
                    "message_count": len(body.get("messages") or []),
                    "tool_names": [
                        (t.get("function") or {}).get("name")
                        for t in body.get("tools") or []
                    ],
                    "check": request_check(body),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        model = str(body.get("model") or "capture")
        if body.get("stream"):
            chunk = {
                "id": "capture-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": REPLY},
                        "finish_reason": "stop",
                    }
                ],
            }
            payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            content_type = "text/event-stream"
        else:
            payload = json.dumps(_completion(model)).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _scripted(self, body: dict[str, Any]) -> None:
        assert SCRIPT is not None
        index = scripted_turn(SCRIPT, body)
        print(
            json.dumps(
                {
                    "kind": "scripted_request",
                    "path": self.path,
                    "turn": index,
                    "header_names": sorted(
                        h.lower() for h in self.headers if h.lower() != "authorization"
                    ),
                    "body": body,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        payload, content_type = scripted_payload(
            index,
            SCRIPT[index],
            str(body.get("model") or "scripted"),
            bool(body.get("stream")),
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> int:
    global SCRIPT
    args = list(sys.argv[1:] if argv is None else argv)
    if "--script" in args:
        at = args.index("--script")
        SCRIPT = load_script(args[at + 1])
        del args[at : at + 2]
    port = int((args or ["8000"])[0])
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
