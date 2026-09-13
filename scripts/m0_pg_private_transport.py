"""Private pipe-only transport for the explicitly authorized PG probe.

stdout is a private binary protocol, never a terminal/report surface.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx2

from scripts.m0.budget import PostgresBudget
from scripts.m0.config import load_config
from scripts.m0.contracts import RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import Fence, StepStore, digest


def main():
    sent_fd = int(sys.argv[1])
    deadline = float(sys.argv[2])
    timeout = min(360, deadline - time.time())
    if timeout <= 0:
        raise ValueError
    raw = sys.stdin.buffer.read(524289)
    if len(raw) > 524288:
        raise ValueError
    body = json.loads(raw)
    if (
        body.get("model") != "deepseek-v4-flash"
        or body.get("thinking") != {"type": "enabled"}
        or body.get("reasoning_effort") != "high"
        or body.get("stream")
        or body.get("max_tokens") != 32768
    ):
        raise ValueError
    config = load_config(Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env"))
    metadata = json.loads(sys.argv[3])
    run = RunContext(
        UUID(metadata["experiment"]),
        UUID(metadata["run"]),
        "deepseek",
        datetime.fromisoformat(metadata["run_deadline"]),
    )
    fence = Fence(
        UUID(metadata["subject"]),
        run,
        metadata["generation"],
        UUID(metadata["owner"]),
        metadata["epoch"],
    )
    store = StepStore(PostgresBudget(DSN))
    sent = False

    def trace(event, info):
        nonlocal sent
        if event in {
            "http11.send_request_headers.started",
            "http11.send_request_body.started",
        }:
            if time.time() >= deadline:
                raise TimeoutError
            check()
        if event == "http11.send_request_body.complete" and not sent:
            release()
            os.write(sent_fd, b"1")
            sent = True
            os.close(sent_fd)

    with store.send_guard(
        fence, UUID(metadata["request"]), input_hash=digest(body)
    ) as (release, check):
        # No retries, redirects, proxy environment or alternate endpoint.
        with httpx2.Client(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=httpx2.HTTPTransport(retries=0, trust_env=False),
        ) as client:
            with client.stream(
                "POST",
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": "Bearer " + config.values["DEEPSEEK_API_KEY"],
                    "Content-Type": "application/json",
                },
                content=raw,
                extensions={"trace": trace},
            ) as response:
                sys.stdout.buffer.write(str(response.status_code).encode() + b"\n")
                sys.stdout.buffer.flush()
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2097152:
                        raise ValueError
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # Do not render provider body, exception, credential or protocol state.
        os._exit(2)
