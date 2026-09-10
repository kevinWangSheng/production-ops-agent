import json
import logging
import os
import sys
from pathlib import Path

root = Path.cwd()
os.environ.clear()
os.environ.update(
    {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root / "tmp/m0-environment"),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "DO_NOT_TRACK": "1",
        "OTEL_SDK_DISABLED": "true",
        "REASONING_EFFORT": "high",
        "LLM_REQUEST_TIMEOUT": "180",
        "OVERRIDE_MAX_OUTPUT_TOKEN": "8192",
        "OVERRIDE_MAX_CONTENT_SIZE": "131072",
    }
)
logging.disable(logging.CRITICAL)
sys.path.insert(
    0,
    str(root / "tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4"),
)
import httpx  # noqa: E402 -- configure isolated upstream environment before imports
from holmes.core.llm import DefaultLLM  # noqa: E402

records = []


def fake(client, req, **kwargs):
    body = json.loads(req.content)
    records.append(
        {
            "url": str(req.url),
            "model": body.get("model"),
            "thinking": body.get("thinking"),
            "reasoning_effort": body.get("reasoning_effort"),
            "max_tokens": body.get("max_tokens"),
            "stream": body.get("stream"),
            "keys": sorted(body),
            "continuation_reasoning_present": "reasoning_content"
            in body["messages"][-2]
            if len(body["messages"]) > 2
            else False,
        }
    )
    return httpx.Response(
        200,
        request=req,
        json={
            "id": "offline",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "offline protocol transport placeholder",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


httpx.Client.send = fake
llm = DefaultLLM(
    model="openai/deepseek-v4-flash",
    api_key="offline-placeholder",
    api_base="https://api.deepseek.com/v1",
    args={
        "num_retries": 0,
        "max_retries": 0,
        "max_tokens": 8192,
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    },
    tracer=None,
)
llm.completion(
    messages=[
        {"role": "system", "content": "offline"},
        {"role": "user", "content": "offline"},
    ],
    stream=False,
)
llm.completion(
    messages=[
        {"role": "user", "content": "offline"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "private-placeholder",
            "tool_calls": [
                {
                    "id": "offline-tool",
                    "type": "function",
                    "function": {"name": "otel_services", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "offline-tool", "content": "{}"},
    ],
    stream=False,
)
token_count = llm.count_tokens([{"role": "user", "content": "offline"}])
print(
    json.dumps(
        {
            "kind": "offline_transport_only",
            "requests": records,
            "token_counter_ready": token_count is not None,
        }
    )
)
