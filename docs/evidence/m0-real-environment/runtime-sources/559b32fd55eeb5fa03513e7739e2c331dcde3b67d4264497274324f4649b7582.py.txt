"""Private pipe-only HTTP worker. Never logs request, headers, or response."""

import base64
import json
import sys
from urllib.parse import urlparse


def collect_response(response, byte_limit):
    chunks = []
    size = 0
    error = None
    try:
        for chunk in response.iter_bytes(chunk_size=65536):
            remaining = byte_limit - size
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
            if len(chunk) > remaining:
                error = "response byte limit exceeded"
                break
    except Exception:
        error = "response body interrupted"
    return {
        "status": response.status_code,
        "headers": {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-encoding", "content-length"}
        },
        "body": base64.b64encode(b"".join(chunks)).decode(),
        "complete": error is None,
        "error": error,
    }


def main():
    import httpx

    p = json.loads(sys.stdin.buffer.read())
    u = urlparse(p["url"])
    permitted = (
        u.scheme == "https"
        and u.hostname == "api.deepseek.com"
        and u.port in (None, 443)
        and u.path in {"/chat/completions", "/v1/chat/completions"}
        and p["method"] == "POST"
    ) or (
        u.scheme == "http"
        and u.hostname == "127.0.0.1"
        and u.port == 18081
        and u.path.startswith("/integrations/m0-otel-20260909/")
        and p["method"] == "GET"
    )
    if not permitted:
        raise ValueError("egress denied")
    with httpx.Client(
        timeout=p["timeout"], follow_redirects=False, trust_env=False
    ) as client:
        with client.stream(
            p["method"],
            p["url"],
            headers=p["headers"],
            content=base64.b64decode(p["body"]),
        ) as response:
            result = collect_response(response, p["byte_limit"])
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": type(exc).__name__}))
