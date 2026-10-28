"""Offline request check before the live Run (#396 acceptance 3).

A fake Chat Completions endpoint on the internal network: it records the JSON
body Holmes/LiteLLM would send to the model API (the Authorization header is
dropped, the key is a dummy anyway) and answers with a fixed final reply so
``holmes ask`` ends after one call. ``request_check`` then asserts that the
outgoing body carries thinking and reasoning effort as configured.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REPLY = "capture endpoint: request recorded"


def request_check(body: dict[str, Any]) -> dict[str, Any]:
    """Which of OpsPilot's request settings the captured body carries.

    LiteLLM may forward ``thinking`` top-level or inside ``extra_body``
    (M0 saw it rejected top-level), so both places count.
    """
    extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    thinking = body.get("thinking", extra.get("thinking"))
    effort = body.get("reasoning_effort", extra.get("reasoning_effort"))
    checks = {
        "thinking_enabled": thinking == {"type": "enabled"},
        "reasoning_effort_high": effort == "high",
        "max_tokens_65536": body.get("max_tokens") == 65536
        or body.get("max_completion_tokens") == 65536,
        "model": body.get("model"),
    }
    return {
        **checks,
        "ok": checks["thinking_enabled"] and checks["reasoning_effort_high"],
    }


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
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


def main(argv: list[str] | None = None) -> int:
    port = int((argv or sys.argv[1:] or ["8000"])[0])
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
