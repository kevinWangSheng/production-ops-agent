"""Run with pinned Holmes Python: real loop/httpx.Request, fake pipe transport only."""

import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import dotenv  # noqa: E402

from scripts.m0_environment import holmes_baseline as wrapper  # noqa: E402

metadata = (
    ROOT / "docs/evidence/m0-real-environment/round-02-provider-models.json"
).read_bytes()
dsml = json.loads(
    (ROOT / "tests/fixtures/m0_environment/normal01_dsml.json").read_text()
)["final_business_content"]


def run_case(final_content, expect_status, final_tools=False, max_steps=4):
    with tempfile.TemporaryDirectory() as directory:
        task_root = Path(directory)
        meta = (
            task_root
            / "docs/evidence/m0-real-environment/round-02-provider-models.json"
        )
        meta.parent.mkdir(parents=True)
        meta.write_bytes(metadata)
        (task_root / "tmp/m0-environment").mkdir(parents=True)
        registry = task_root / "registry.json"
        registry.write_text(
            json.dumps({"integration_id": "m0-otel-20260909", "containers": []})
        )
        scope = task_root / "scope.json"
        scope.write_text(
            json.dumps(
                {
                    "integration_id": "m0-otel-20260909",
                    "services": ["checkout"],
                    "metrics_scope": "integration",
                    "window": {"start": 1788976626, "end": 1788976926},
                    "policy_revision": "offline-only",
                    "deployment_registry_file": str(registry),
                }
            )
        )
        question = task_root / "question.json"
        question.write_text(
            json.dumps({"question": "Investigate the observed window."})
        )
        model_requests = []

        def fake_child(command, data, wall_seconds):
            wire = json.loads(data)
            if "api.deepseek.com" in wire["url"]:
                payload = json.loads(base64.b64decode(wire["body"]))
                assert int(wire["headers"]["content-length"]) == len(
                    base64.b64decode(wire["body"])
                )
                model_requests.append(payload)
                ordinal = len(model_requests)
                message = {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": f"synthetic-private-{ordinal}",
                }
                if ordinal < max_steps:
                    name, arguments = [
                        ("otel_services", {}),
                        ("otel_metrics", {"query": "up"}),
                        ("otel_logs", {"service": "checkout"}),
                    ][ordinal - 1]
                    message["tool_calls"] = [
                        {
                            "id": f"call-{ordinal}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                    finish = "tool_calls"
                else:
                    message["content"] = final_content
                    finish = "stop"
                    if final_tools:
                        message["tool_calls"] = [
                            {
                                "id": "closed-call",
                                "type": "function",
                                "function": {
                                    "name": "otel_services",
                                    "arguments": "{}",
                                },
                            }
                        ]
                        finish = "tool_calls"
                response = {
                    "id": f"offline-{ordinal}",
                    "object": "chat.completion",
                    "created": 1788976926,
                    "model": "deepseek-flash",
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish}
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 100,
                    },
                }
            elif "/services" in wire["url"]:
                response = {
                    "integration_id": "m0-otel-20260909",
                    "services": ["checkout"],
                }
            elif "/metrics" in wire["url"]:
                response = {
                    "integration_id": "m0-otel-20260909",
                    "data": {
                        "data": {
                            "result": [
                                {
                                    "metric": {"service_name": "checkout"},
                                    "value": [1788976926, "1"],
                                }
                            ]
                        }
                    },
                }
            else:
                response = {
                    "integration_id": "m0-otel-20260909",
                    "data": {"hits": {"hits": [], "total": {"value": 0}}},
                }
            return json.dumps(
                {
                    "status": 200,
                    "headers": {},
                    "body": base64.b64encode(json.dumps(response).encode()).decode(),
                    "complete": True,
                }
            ).encode()

        argv = [
            "holmes_baseline.py",
            "--report-version",
            "m0-report-v1",
            "--run-id",
            "offlinewire",
            "--phase",
            "normal",
            "--max-steps",
            str(max_steps),
            "--question-file",
            str(question),
            "--scope-file",
            str(scope),
        ]
        with (
            patch.object(wrapper, "ROOT", task_root),
            patch.object(wrapper, "run_child", fake_child),
            patch.object(
                dotenv,
                "dotenv_values",
                return_value={"DEEPSEEK_API_KEY": "offline-fake-not-a-secret"},
            ),
            patch.object(sys, "argv", argv),
        ):
            wrapper.main()
        result = json.loads(
            (
                task_root
                / "tmp/m0-environment/holmes-runs/offlinewire/result-business.json"
            ).read_text()
        )
        assert result["status"] == expect_status, result
        assert len(model_requests) == max_steps
        assert result["tool_queries"] == max_steps - 1
        assert all(
            p.get("tool_choice") == "auto" and "response_format" not in p
            for p in model_requests[:-1]
        )
        final = model_requests[-1]
        assert "tool_choice" not in final and "tools" not in final
        assert final["response_format"] == {"type": "json_object"}
        assert "CLOSED" in final["messages"][-1]["content"]
        private = [
            m["reasoning_content"]
            for m in final["messages"]
            if m.get("role") == "assistant"
        ]
        assert private == [f"synthetic-private-{i}" for i in range(1, max_steps)]
        assert result["report_request_id"] == f"offlinewire-http-{max_steps}"
        print(
            json.dumps(
                {
                    "offline_probe": "pass",
                    "case": expect_status,
                    "model_requests": max_steps,
                    "private_roundtrip": True,
                    "real_http": 0,
                }
            )
        )


report = {
    "schema_version": "m0-report-v1",
    "assessment_status": "completed",
    "conclusion": "partial",
    "summary": "Observed data, with gaps.",
    "claims": [
        {
            "kind": "fact",
            "text": "The service inventory includes checkout.",
            "evidence_ids": ["offlinewire-e1"],
        }
    ],
    "gaps": ["No independent baseline."],
    "next_steps": [],
}
run_case(json.dumps(report), "investigation_returned")
run_case(dsml, "incomplete")

run_case("", "incomplete", final_tools=True)

run_case(json.dumps(report), "investigation_returned", max_steps=3)
