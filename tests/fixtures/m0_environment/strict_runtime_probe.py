"""Run with pinned Holmes Python: real loop/httpx.Request, fake pipe transport only."""

import base64
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import dotenv  # noqa: E402

from scripts.m0_environment import holmes_baseline as wrapper  # noqa: E402

wrapper.UPSTREAM = Path(os.environ["HOLMES_TEST_UPSTREAM"])

metadata = (
    ROOT / "docs/evidence/m0-real-environment/round-02-provider-models.json"
).read_bytes()
dsml = json.loads(
    (ROOT / "tests/fixtures/m0_environment/normal01_dsml.json").read_text()
)["final_business_content"]


def run_case(
    final_content,
    expect_status,
    final_tools=False,
    max_steps=4,
    use_initial_timing=False,
):
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
                    "control_generation": 7,
                    "deployment_registry_file": str(registry),
                }
            )
        )
        policy = task_root / "policy.json"
        policy.write_text(
            json.dumps(
                [
                    {
                        "id": "historical",
                        "revision": "offline-v1",
                        "integration_id": "m0-otel-20260909",
                        "interfaces": [
                            "otel_services",
                            "otel_metrics",
                            "otel_logs",
                            "otel_traces",
                        ],
                        "mode": "historical_window",
                        "reference_rule": "response_received_at",
                        "window": {
                            "start": datetime.fromtimestamp(
                                1788976626, timezone.utc
                            ).isoformat(),
                            "end": datetime.fromtimestamp(
                                1788976926, timezone.utc
                            ).isoformat(),
                        },
                        "all_authorized_targets": True,
                        "scope_revision": "offline-only",
                    }
                ]
            )
        )
        initial = {
            "evidence_id": "initial-e1",
            "tool": "otel_logs",
            "http_status": 200,
            "actual_sources": {"services": ["checkout"]},
            "trusted_access_scope": json.loads(scope.read_text()),
            "data": {
                "displayed_logs": [
                    {
                        "timestamp": datetime.fromtimestamp(
                            1788976700, timezone.utc
                        ).isoformat(),
                        "service": "checkout",
                        "body": "initial visible event",
                    }
                ]
            },
        }
        question = task_root / "question.json"
        question.write_text(
            json.dumps(
                {
                    "question": "Investigate the observed window.",
                    "business_tool_views": [initial]
                    if use_initial_timing or final_content == "unauthorized-initial"
                    else [],
                }
            )
        )
        if final_content == "unauthorized-initial":
            initial["actual_sources"]["services"] = ["outside-scope"]
            initial["data"]["displayed_logs"][0]["service"] = "outside-scope"
            question.write_text(
                json.dumps(
                    {"question": "Investigate", "business_tool_views": [initial]}
                )
            )
        model_requests = []
        model_request_bytes = []

        def fake_child(command, data, wall_seconds):
            wire = json.loads(data)
            if "api.deepseek.com" in wire["url"]:
                payload = json.loads(base64.b64decode(wire["body"]))
                assert int(wire["headers"]["content-length"]) == len(
                    base64.b64decode(wire["body"])
                )
                model_requests.append(payload)
                model_request_bytes.append(base64.b64decode(wire["body"]))
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
                    contexts = []
                    for item in payload["messages"]:
                        if item.get("role") != "user":
                            continue
                        try:
                            value = json.loads(item["content"])
                        except ValueError:
                            continue
                        if (
                            isinstance(value, dict)
                            and value.get("type") == "opspilot-evidence-context-v4"
                        ):
                            contexts.append(value)
                    assert len(contexts) == 1
                    ctx = contexts[0]
                    if final_content == "valid":
                        binding = ctx["view_bindings"]["offlinewire-e3"]
                        report_value = {
                            "schema_version": "m0-report-v2",
                            "assessment_status": "completed",
                            "conclusion": "partial",
                            "summary": "One displayed event is supported; remaining coverage unknown.",
                            "claims": [
                                {
                                    "kind": "fact",
                                    "text": "The displayed checkout event has a recorded timestamp.",
                                    "evidence_ids": ["offlinewire-e3"],
                                    "target_refs": binding["target_refs"],
                                    "time_scope_ref": binding["time_scope_refs"][0],
                                }
                            ],
                            "gaps": ["Limited sample."],
                            "next_steps": ["Human review only."],
                        }
                        message["content"] = json.dumps(report_value)
                    elif final_content == "invalid-target":
                        report_value = {
                            "schema_version": "m0-report-v2",
                            "assessment_status": "completed",
                            "conclusion": "partial",
                            "summary": "Invalid target must be rejected.",
                            "claims": [
                                {
                                    "kind": "fact",
                                    "text": "Fabricated target.",
                                    "evidence_ids": ["offlinewire-e3"],
                                    "target_refs": ["target:invented"],
                                    "time_scope_ref": "historical",
                                }
                            ],
                            "gaps": [],
                            "next_steps": [],
                        }
                        message["content"] = json.dumps(report_value)
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
                    "data": {
                        "hits": {
                            "hits": [
                                {
                                    "_id": "event-1",
                                    "_source": {
                                        "@timestamp": datetime.fromtimestamp(
                                            1788976750, timezone.utc
                                        ).isoformat(),
                                        "resource": {"service.name": "checkout"},
                                        "body": "displayed event",
                                    },
                                }
                            ],
                            "total": {"value": 1},
                        }
                    },
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
            "--time-policy-file",
            str(policy),
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
        if use_initial_timing:
            timing_file = task_root / "initial-timings.json"
            timing = wrapper.source_timing({}, initial)
            now = datetime.now(timezone.utc).isoformat()
            timing.update(operation_started_at=now, collection_completed_at=now)
            timing_file.write_text(
                json.dumps(
                    {
                        "initial-e1": {
                            "view_hash": wrapper.canonical_hash(initial),
                            "timing": timing,
                        }
                    }
                )
            )
            argv.extend(["--initial-timings-file", str(timing_file)])
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
        if final_content == "invalid-target":
            assert result["final_report"]["claims"][0]["target_refs"] == [
                "target:invented"
            ]
        if final_content == "unauthorized-initial" or use_initial_timing:
            assert len(model_requests) == 0
            assert result["initial_evidence_status"] == "unknown"
            print(
                json.dumps(
                    {
                        "offline_probe": "pass",
                        "case": "unauthorized-initial",
                        "model_requests": 0,
                        "real_http": 0,
                    }
                )
            )
            return
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
        folder = task_root / "tmp/m0-environment/holmes-runs/offlinewire"
        deliveries = json.loads((folder / "delivered-business.json").read_text())
        captures = json.loads((folder / "report-capture-business.json").read_text())
        timing_records = json.loads((folder / "evidence-timings.json").read_text())
        assert (
            json.loads((folder / "time-policies.json").read_text())
            == deliveries[-1]["evidence_context"]["time_policies"]
        )
        for ordinal, receipt in enumerate(deliveries):
            assert (
                receipt["actual_request_sha256"]
                == hashlib.sha256(model_request_bytes[ordinal]).hexdigest()
            )
            assert receipt["business_messages_sha256"] == wrapper.canonical_hash(
                receipt["messages"]
            )
            assert receipt["evidence_context_sha256"] == wrapper.canonical_hash(
                receipt["evidence_context"]
            )
            for evidence_id, binding in receipt["evidence_context"][
                "view_bindings"
            ].items():
                assert timing_records[evidence_id] == {
                    "view_hash": binding["view_hash"],
                    "timing": binding["timing"],
                }
            assert receipt["evidence_context"]["run_id"] == "offlinewire"
            assert receipt["control_generation"] == 7
            assert datetime.fromisoformat(
                receipt["dispatch_started_at"]
            ) <= datetime.fromisoformat(receipt["response_received_at"])
        assert deliveries[0]["evidence_context"]["view_bindings"] == {}
        last_context = deliveries[-1]["evidence_context"]
        assert last_context == json.loads(final["messages"][-2]["content"])
        assert (
            last_context["view_bindings"]["offlinewire-e3"]["timing"][
                "source_time_basis"
            ]
            == "event_time"
        )
        assert (
            last_context["view_bindings"]["offlinewire-e2"]["timing"][
                "source_time_basis"
            ]
            == "unknown"
        )
        for observation in json.loads((folder / "observations.json").read_text()):
            assert observation["operation_started_at"] == observation["observed_at"]
            assert (
                datetime.fromisoformat(observation["operation_started_at"])
                <= datetime.fromisoformat(observation["collection_completed_at"])
                <= datetime.fromisoformat(deliveries[-1]["dispatch_started_at"])
            )
        assert captures["content"] == result["final_business_content"]
        assert (
            captures["response_received_at"] == deliveries[-1]["response_received_at"]
        )
        assert captures["control_generation"] == 7
        assert "synthetic-private" not in json.dumps(deliveries)
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


run_case("valid", "investigation_returned")
run_case("invalid-target", "incomplete")

run_case("valid", "incomplete", use_initial_timing=True)
run_case("unauthorized-initial", "incomplete")
