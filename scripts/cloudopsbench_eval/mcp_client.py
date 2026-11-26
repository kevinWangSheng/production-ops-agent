"""Minimal MCP client for the benchmark's own server (#871 U1).

The benchmark serves its tools over MCP Streamable HTTP with
``json_response=True`` and ``stateless_http=True``
(``cloudopsbench_mcp/server.py``): every request is one JSON-RPC POST to
``/mcp`` answered by one JSON body, with no session to open first. This
client speaks only that: ``tools/list`` and ``tools/call``, standard library
only, loopback only, no proxy (the README warns a proxy breaks it), no
redirects. An SSE-framed answer is still read (its last ``data:`` event), in
case a server version ignores ``json_response``.
"""

from __future__ import annotations

import itertools
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

PROTOCOL_VERSION = "2025-06-18"
MAX_REPLY_BYTES = 8 * 1024 * 1024


class McpError(RuntimeError):
    """``code`` is ``MCP_UNREACHABLE``, ``MCP_TIMEOUT``, ``MCP_HTTP_<n>``,
    ``MCP_BAD_REPLY``, ``MCP_RPC_ERROR`` or ``MCP_REPLY_TOO_LARGE``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def require_loopback(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
        raise McpError("MCP_NOT_LOOPBACK")


def _decode(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8")
    if "text/event-stream" in content_type:
        data = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        ]
        if not data:
            raise McpError("MCP_BAD_REPLY", "empty event stream")
        text = data[-1]
    return json.loads(text)


class McpClient:
    def __init__(self, url: str, *, opener: Any = None) -> None:
        require_loopback(url)
        self.url = url
        self._opener = opener or _OPENER
        self._ids = itertools.count(1)

    def _rpc(self, method: str, params: Mapping[str, Any], timeout: float) -> Any:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method,
             "params": dict(params)}
        ).encode("utf-8")  # fmt: skip
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_REPLY_BYTES + 1)
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            raise McpError(f"MCP_HTTP_{error.code}") from None
        except (TimeoutError, socket.timeout):
            raise McpError("MCP_TIMEOUT") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise McpError("MCP_TIMEOUT") from None
            raise McpError("MCP_UNREACHABLE", type(error.reason).__name__) from None
        except (ConnectionError, OSError) as error:
            raise McpError("MCP_UNREACHABLE", type(error).__name__) from None
        if len(raw) > MAX_REPLY_BYTES:
            raise McpError("MCP_REPLY_TOO_LARGE")
        try:
            reply = _decode(raw, content_type)
        except (UnicodeDecodeError, ValueError):
            raise McpError("MCP_BAD_REPLY") from None
        if not isinstance(reply, dict):
            raise McpError("MCP_BAD_REPLY")
        if "error" in reply:
            raise McpError("MCP_RPC_ERROR", str(reply["error"])[:200])
        if "result" not in reply:
            raise McpError("MCP_BAD_REPLY")
        return reply["result"]

    def list_tools(self, timeout: float = 10.0) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {}, timeout)
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpError("MCP_BAD_REPLY")
        return tools

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], timeout: float
    ) -> tuple[str, bool]:
        """``(text, is_error)``: the text parts joined, and the server's
        ``isError`` (an adapter error such as invalid arguments)."""
        result = self._rpc(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout
        )
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise McpError("MCP_BAD_REPLY")
        parts = [
            str(item.get("text", ""))
            for item in result["content"]
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "".join(parts), bool(result.get("isError"))
