"""Upstream arm: pinned HolmesGPT loop over the same frozen replay tool face.

Run with the Holmes venv. Default toolsets, skills, shell, kubectl, curl, tracing
and telemetry are not loaded; only the replay Toolset is registered. The API key
comes from the first line of stdin and is handed to the upstream LLM object only.
HTTP egress is guarded at httpx.Client.send: only the DeepSeek chat endpoint,
at most --max-http requests, usage captured, private reasoning fields scrubbed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
from scripts.m0_lab.round07.replay_tools import (  # noqa: E402
    ReplayState,
    canonical_hash,
    load_packet,
    tool_definitions,
)

UPSTREAM = Path(
    "/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/"
    "holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4"
)
MODEL = "deepseek-v4-flash"
MAX_TOKENS = 8192
REQUEST_SECONDS = 360
RUN_SECONDS = 900
PRIVATE_FIELDS = (
    "reasoning_content",
    "reasoning",
    "thinking_blocks",
    "provider_specific_fields",
)
ENV = {
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "LITELLM_TELEMETRY": "False",
    "DO_NOT_TRACK": "1",
    "OTEL_SDK_DISABLED": "true",
    "HOLMES_LANGFUSE_ATTRIBUTES": "false",
    "LOG_LLM_USAGE_RESPONSE": "false",
    "HOLMES_DISABLE_VISION": "true",
    "LLM_REQUEST_TIMEOUT": str(REQUEST_SECONDS),
    "OVERRIDE_MAX_OUTPUT_TOKEN": str(MAX_TOKENS),
    "OVERRIDE_MAX_CONTENT_SIZE": "131072",
    "REASONING_EFFORT": "high",
    "ENABLE_CONVERSATION_HISTORY_COMPACTION": "false",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def code_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "holmes").rglob("*.py")):
        digest.update(
            str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0"
        )
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-http", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    key = "" if args.dry_run else sys.stdin.readline().strip()
    if not args.dry_run and not key:
        print(
            json.dumps(
                {"status": "failed", "failure": "trusted credential unavailable"}
            )
        )
        return 2
    packet = load_packet(args.packet)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    retained = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "TMPDIR", "SSL_CERT_FILE"}
    }
    os.environ.clear()
    os.environ.update(retained)
    os.environ.update(ENV)
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(UPSTREAM))
    import httpx
    import litellm
    from holmes.core.llm import DefaultLLM
    from holmes.core.prompt import build_system_prompt
    from holmes.core.tool_calling_llm import ToolCallingLLM
    from holmes.core.tools import (
        StructuredToolResult,
        StructuredToolResultStatus,
        Tool,
        ToolParameter,
        Toolset,
        ToolsetStatusEnum,
    )
    from holmes.core.tools_utils.tool_executor import ToolExecutor
    from holmes.plugins.toolsets.investigator.core_investigation import (
        CoreInvestigationToolset,
    )
    from holmes.utils.stream import StreamEvents

    litellm.telemetry = False
    litellm.callbacks = []
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.num_retries = 0

    state = ReplayState(packet)
    attempts: list[dict] = []
    original_send = httpx.Client.send

    def guarded_send(client, request, **kwargs):
        url = request.url
        if not (
            url.scheme == "https"
            and url.host == "api.deepseek.com"
            and url.path in {"/chat/completions", "/v1/chat/completions"}
            and request.method == "POST"
        ):
            raise RuntimeError("HTTP egress denied")
        if len(attempts) >= args.max_http:
            raise RuntimeError("Run HTTP request budget reached")
        if args.dry_run:
            raise RuntimeError("dry run: model egress denied")
        body = json.loads(request.content)
        if key in request.content.decode("utf-8", "replace") and key:
            raise RuntimeError("CREDENTIAL_LIKE_EVIDENCE")
        attempt = {
            "ordinal": len(attempts) + 1,
            "request_bytes": len(request.content),
            "message_count": len(body.get("messages", [])),
            "tool_schema_count": len(body.get("tools") or []),
            "tool_choice": body.get("tool_choice"),
            "request_model": body.get("model"),
            "max_tokens": body.get("max_tokens"),
            "thinking": body.get("thinking"),
            "reasoning_effort": body.get("reasoning_effort"),
        }
        attempts.append(attempt)
        started = time.monotonic()
        kwargs["follow_redirects"] = False
        response = original_send(client, request, **kwargs)
        raw = response.content
        attempt.update(
            http_status=response.status_code,
            response_bytes=len(raw),
            response_sha256=sha256(raw),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            attempt["usage"] = payload.get("usage")
            attempt["response_model"] = payload.get("model")
            choice = (payload.get("choices") or [{}])[0]
            if isinstance(choice, dict):
                attempt["finish_reason"] = choice.get("finish_reason")
                message = choice.get("message") or {}
                attempt["tool_call_count"] = len(message.get("tool_calls") or [])
            (out / f"response-{attempt['ordinal']}-business.json").write_text(
                json.dumps(scrub(payload), ensure_ascii=False, indent=2)
            )
        (out / "attempts.json").write_text(
            json.dumps(attempts, ensure_ascii=False, indent=2)
        )
        return response

    httpx.Client.send = guarded_send

    class ReplayTool(Tool):
        def get_parameterized_one_liner(self, params):
            return self.name

        def _invoke(self, params, context):
            ok, view = state.dispatch(self.name, params)
            (out / "tool-calls.json").write_text(
                json.dumps(state.calls, ensure_ascii=False, indent=2)
            )
            return StructuredToolResult(
                status=StructuredToolResultStatus.SUCCESS
                if ok
                else StructuredToolResultStatus.ERROR,
                data=json.dumps(view, ensure_ascii=False),
            )

    toolset = Toolset(
        name="m0_otel_readonly_replay",
        description=(
            "Read-only frozen telemetry views for the fixed authorized OpenTelemetry Demo instance "
            f"{packet['scope']['integration_id']}; no mutation, shell, kubectl or network."
        ),
        enabled=True,
        status=ToolsetStatusEnum.ENABLED,
        tools=[
            ReplayTool(
                name=tool["name"],
                description=tool["description"],
                parameters={
                    k: ToolParameter(description=v["description"])
                    for k, v in tool["parameters"].items()
                },
            )
            for tool in tool_definitions(packet)
        ],
    )
    # Upstream's own in-memory TodoWrite planner (no external effect) is kept because the
    # default prompt template mandates it; every other default toolset stays unloaded.
    planner = CoreInvestigationToolset()
    planner.status = ToolsetStatusEnum.ENABLED
    active = [toolset, planner]
    prompt = build_system_prompt(active, None, None, None, False, {})
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": packet["question"]},
    ]
    schemas = [t.get_openai_format() for ts in active for t in ts.tools]
    (out / "input-business.json").write_text(
        json.dumps(
            {"messages": messages, "tools": schemas}, ensure_ascii=False, indent=2
        )
    )
    config = {
        "arm": "upstream",
        "upstream_commit": UPSTREAM.name.removeprefix("holmesgpt-"),
        "upstream_code_sha256": code_digest(UPSTREAM),
        "litellm_version": getattr(litellm, "version", None)
        or "see holmes-installed-versions.json",
        "model": "openai/" + MODEL,
        "api_base": "https://api.deepseek.com/v1",
        "llm_args": {
            "num_retries": 0,
            "max_retries": 0,
            "max_tokens": MAX_TOKENS,
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
        },
        "max_steps": args.max_http,
        "toolsets_loaded": [ts.name for ts in active],
        "toolsets_not_loaded": "all upstream default toolsets (bash, kubectl, curl/internet, prometheus, docker, ...), skills, MCP",
        "system_prompt_source": "holmes.core.prompt.build_system_prompt default template, no additions",
        "prompt_sha256": sha256(prompt.encode()),
        "tool_schema_sha256": canonical_hash(schemas),
        "env_overrides": ENV,
        "compaction": "disabled",
        "trace": "disabled",
        "runner_sha256": sha256(HERE.read_bytes()),
    }
    config["config_sha256"] = canonical_hash(config)
    (out / "holmes-config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2)
    )
    result = {
        "arm": "upstream",
        "run_id": args.run_id,
        "packet_id": packet["packet_id"],
        "packet_sha256": canonical_hash(packet),
        "holmes_config_sha256": config["config_sha256"],
        "max_http": args.max_http,
        "attempts": attempts,
        "tool_calls": state.calls,
        "status": "incomplete",
        "final_content": None,
        "finish_reason": None,
    }
    if args.dry_run:
        result["status"] = "dry_run"
        (out / "result-business.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    "arm": "upstream",
                    "run_id": args.run_id,
                    "status": "dry_run",
                    "http_count": 0,
                }
            )
        )
        return 0
    llm = DefaultLLM(
        model="openai/" + MODEL,
        api_key=key,
        api_base="https://api.deepseek.com/v1",
        args=dict(config["llm_args"]),
        tracer=None,
    )
    loop = ToolCallingLLM(
        ToolExecutor(active),
        max_steps=args.max_http,
        llm=llm,
        tool_results_dir=None,
        tracer=None,
    )
    deadline = time.monotonic() + RUN_SECONDS
    try:
        for event in loop.call_stream(msgs=messages):
            if time.monotonic() > deadline:
                raise TimeoutError("run deadline")
            if event.event == StreamEvents.ANSWER_END:
                content = event.data.get("content")
                result["final_content"] = content
                result["finish_reason"] = event.data.get("metadata", {}).get(
                    "finish_reason"
                )
                result["status"] = (
                    "final_returned"
                    if isinstance(content, str) and content.strip()
                    else "failed"
                )
    except Exception as exc:  # failure stays a failure; message may hold provider text, keep the type only
        result["status"] = "failed"
        result["failure"] = type(exc).__name__ + ": " + str(exc)[:200]
    result["http_count"] = len(attempts)
    (out / "result-business.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in ("arm", "run_id", "status", "http_count", "finish_reason")
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
