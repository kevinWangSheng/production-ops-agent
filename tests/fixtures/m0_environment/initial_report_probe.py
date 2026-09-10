"""One actual Holmes step with fake transport, then the strict external checker."""

import base64
import json
import os
import runpy
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import dotenv  # noqa: E402

from scripts.m0.holmes_bridge import load_packet  # noqa: E402
from scripts.m0.outcomes_v3 import ProjectionContext  # noqa: E402
from scripts.m0.outcomes_v4 import check_outcome  # noqa: E402
from scripts.m0_environment import holmes_baseline as wrapper  # noqa: E402

wrapper.UPSTREAM = Path(os.environ["HOLMES_TEST_UPSTREAM"])
MIXED_FINAL_TOOL_CALL = "--mixed-final-tool-call" in sys.argv
make_bundle = runpy.run_path(str(ROOT / "tests/test_m0_initial_evidence.py"))["bundle"]
metadata = (
    ROOT / "docs/evidence/m0-real-environment/round-02-provider-models.json"
).read_bytes()

with tempfile.TemporaryDirectory() as directory:
    task = Path(directory)
    source = task / "source"
    source.mkdir()
    manifest, scope, raw, view = make_bundle(source)
    value = json.loads(manifest.read_text())
    registry = value["projection_context"]["registry"]
    # A source integration target is enough for the fact; subject remains exact Compose.
    registry["containers"] = [
        {
            "container_id": "container-checkout",
            "hostname": "checkout-host",
            "image_id": "sha256:fixture",
            "labels": {
                "com.docker.compose.service": "checkout",
                "com.docker.compose.config-hash": "config",
            },
        }
    ]
    # Rebuild the fixture with this registry through the same verified projector.
    from scripts.m0.holmes_bridge import canonical_hash, replay_projection
    from scripts.m0_environment.round02 import save

    value["projection_context"]["registry_hash"] = canonical_hash(registry)
    scope["deployment_registry_sha256"] = canonical_hash(registry)
    raw["trusted_access_scope"] = scope
    context = ProjectionContext.model_validate_json(
        json.dumps(value["projection_context"])
    )
    view = replay_projection(raw, context, revision="m0-02-logs-v3")
    save(source / "raw.json", raw)
    save(source / "view.json", view)
    import hashlib

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    original_manifest = json.loads((source / "manifest.json").read_text())
    original_manifest.update(
        raw_sha256=canonical_hash(raw),
        view_sha256=canonical_hash(view),
        raw_file_sha256=sha(source / "raw.json"),
        view_file_sha256=sha(source / "view.json"),
        scope=scope,
    )
    save(source / "manifest.json", original_manifest)
    value["entries"][0].update(
        raw_file_sha256=sha(source / "raw.json"),
        view_file_sha256=sha(source / "view.json"),
        manifest_file_sha256=sha(source / "manifest.json"),
    )
    save(manifest, value)
    registry_path = task / "registry.json"
    save(registry_path, registry)
    scope.update(
        metrics_scope="integration",
        control_generation=0,
        deployment_registry_file=str(registry_path),
    )
    scope_path = task / "scope.json"
    save(scope_path, scope)
    policy_path = task / "policies.json"
    save(
        policy_path,
        [
            {
                "id": "history",
                "revision": "p",
                "integration_id": registry["integration_id"],
                "interfaces": ["otel_logs"],
                "mode": "historical_window",
                "window": {
                    k: datetime.fromtimestamp(v, timezone.utc).isoformat()
                    for k, v in scope["window"].items()
                },
                "reference_rule": "response_received_at",
                "all_authorized_targets": True,
                "scope_revision": "p",
            }
        ],
    )
    question = task / "question.json"
    question.write_bytes(
        (
            json.dumps(
                {
                    "run_id": "reportonly",
                    "request": "Review the preserved event",
                    "investigation_subject": {
                        "service": "checkout",
                        "container_id": "container-checkout",
                        "image_id": "sha256:fixture",
                        "config_revision": "config",
                    },
                },
                indent=2,
            )
            + "\r\n"
        ).encode()
    )
    meta = task / "docs/evidence/m0-real-environment/round-02-provider-models.json"
    meta.parent.mkdir(parents=True)
    meta.write_bytes(metadata)
    (task / "tmp/m0-environment").mkdir(parents=True)
    calls = []

    def fake_child(command, data, wall_seconds):
        wire = json.loads(data)
        assert "api.deepseek.com" in wire["url"]
        payload = json.loads(base64.b64decode(wire["body"]))
        calls.append(payload)
        context = json.loads(payload["messages"][-2]["content"])
        binding = context["view_bindings"]["producer-e1"]
        report = {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "partial",
            "summary": "One preserved visible event.",
            "claims": [
                {
                    "kind": "fact",
                    "text": "A checkout log event is present.",
                    "evidence_ids": ["producer-e1"],
                    "target_refs": binding["target_refs"],
                    "time_scope_ref": "history",
                }
            ],
            "gaps": ["Limited evidence."],
            "next_steps": ["Human review."],
        }
        response = {
            "id": "fake",
            "object": "chat.completion",
            "created": 1789002060,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(report),
                        "reasoning_content": "synthetic-private",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 100,
            },
        }
        if MIXED_FINAL_TOOL_CALL:
            response["choices"][0]["message"]["tool_calls"] = [
                {
                    "id": "forbidden-final-call",
                    "type": "function",
                    "function": {"name": "otel_services", "arguments": "{}"},
                }
            ]
        return json.dumps(
            {
                "status": 200,
                "headers": {},
                "body": base64.b64encode(json.dumps(response).encode()).decode(),
                "complete": True,
            }
        ).encode()

    argv = [
        "wrapper",
        "--run-id",
        "reportonly",
        "--phase",
        "report",
        "--max-steps",
        "1",
        "--question-file",
        str(question),
        "--scope-file",
        str(scope_path),
        "--time-policy-file",
        str(policy_path),
        "--initial-evidence-manifest",
        str(manifest),
    ]
    with (
        patch.object(wrapper, "ROOT", task),
        patch.object(wrapper, "run_child", fake_child),
        patch.object(
            dotenv, "dotenv_values", return_value={"DEEPSEEK_API_KEY": "synthetic"}
        ),
        patch.object(sys, "argv", argv),
    ):
        wrapper.main()
    folder = task / "tmp/m0-environment/holmes-runs/reportonly"
    assert (
        json.loads((folder / "configuration.json").read_text())[
            "report_instruction_sha256"
        ]
        == hashlib.sha256(
            wrapper.report_instruction(final=True, version="m0-report-v2").encode()
        ).hexdigest()
    )
    assert (
        json.loads((folder / "delivered-business.json").read_text())[0]["final_phase"]
        is True
    )
    assert len(calls) == 1
    assert json.loads((folder / "observations.json").read_text()) == []
    imported = json.loads((folder / "initial-evidence.json").read_text())
    copied_context = ProjectionContext.model_validate_json(
        json.dumps(imported["projection_context"])
    )
    scenario, outcome = load_packet(
        folder,
        projection_source_sha256=copied_context.source_sha256,
        projection_source_path=copied_context.source_path,
        projection_dependencies=copied_context.dependencies,
    )
    errors = check_outcome(scenario, outcome)
    if MIXED_FINAL_TOOL_CALL:
        result = json.loads((folder / "result-business.json").read_bytes())
        assert result["status"] == "incomplete"
        assert result["report_validation_error"] == "final response contains tool calls"
        observed = json.loads((folder / "response-1-business.json").read_bytes())
        assert observed["choices"][0]["finish_reason"] == "stop"
        assert result["final_business_content"] is None
        assert outcome.report_content == observed["choices"][0]["content"]
        assert outcome.report.model_dump(mode="json") == json.loads(
            outcome.report_content
        )
        assert scenario.trusted.report_capture is None
        assert "UNACCEPTED_CANDIDATE_FROM_RESPONSE" in outcome.handoff_reasons
        assert "RUNNER_FINAL_CONTENT_NULL" in outcome.handoff_reasons
        assert scenario.trusted.execution == outcome.execution == "blocked"
        assert (
            outcome.handoff
            and "RUNTIME_REPORT_VALIDATION_FAILED" in outcome.handoff_reasons
        )
        assert len(scenario.trusted.artifacts) == len(scenario.trusted.deliveries) == 1
        assert "ASSESSMENT_EXECUTION_MISMATCH" in errors
        print(
            json.dumps(
                {
                    "status": "mixed_final_protocol_rejected",
                    "real_http": 0,
                    "fake_model_steps": len(calls),
                    "report_preserved": True,
                    "execution": outcome.execution,
                    "violations": errors,
                }
            )
        )
        raise SystemExit(0)
    assert errors == [], errors
    assert len(scenario.agent_input.initial_views) == 1
    assert scenario.trusted.observed_actions == []
    assert (folder / "question-original.txt").read_bytes() == question.read_bytes()
    preserved_report = outcome.report.model_dump(mode="json")
    preserved_content = outcome.report_content
    index_path = folder / "initial-evidence.json"
    original_index = index_path.read_bytes()
    negative_results = []
    for case in ("missing", "bad-hash", "duplicate", "mixed"):
        value = json.loads(original_index)
        if case == "missing":
            index_path.unlink()
        else:
            if case == "bad-hash":
                value["entries"][0]["raw_file_sha256"] = "0" * 64
            elif case == "duplicate":
                value["entries"].append(value["entries"][0])
            else:
                value["projection_context"]["source_sha256"] = "0" * 64
            index_path.write_text(json.dumps(value))
        invalid_scenario, invalid_outcome = load_packet(
            folder,
            projection_source_sha256=copied_context.source_sha256,
            projection_source_path=copied_context.source_path,
            projection_dependencies=copied_context.dependencies,
        )
        invalid_errors = check_outcome(invalid_scenario, invalid_outcome)
        assert "UNVERIFIED_INITIAL_EVIDENCE" in invalid_errors, (case, invalid_errors)
        assert invalid_outcome.report.model_dump(mode="json") == preserved_report
        assert invalid_outcome.report_content == preserved_content
        negative_results.append(
            {"case": case, "unverified": True, "full_fact_report_preserved": True}
        )
        index_path.write_bytes(original_index)
    print(json.dumps({"negative_report_only_cases": negative_results, "real_http": 0}))
    scope_gate_results = []
    for case, override in (
        (
            "deny-window",
            {
                "window": {
                    "start": scope["window"]["start"] + 1,
                    "end": scope["window"]["end"],
                }
            },
        ),
        ("deny-interface", {"interfaces": ["otel_metrics"]}),
    ):
        denied_scope = {**scope, **override}
        denied_scope_path = task / (case + "-scope.json")
        save(denied_scope_path, denied_scope)
        denied_question = task / (case + "-question.json")
        question_value = json.loads(question.read_bytes())
        question_value["run_id"] = case
        denied_question.write_text(json.dumps(question_value))
        denied_argv = [
            "wrapper",
            "--run-id",
            case,
            "--phase",
            "report",
            "--max-steps",
            "1",
            "--question-file",
            str(denied_question),
            "--scope-file",
            str(denied_scope_path),
            "--time-policy-file",
            str(policy_path),
            "--initial-evidence-manifest",
            str(manifest),
        ]
        with (
            patch.object(wrapper, "ROOT", task),
            patch.object(
                wrapper,
                "run_child",
                side_effect=AssertionError("transport must not run"),
            ) as transport,
            patch.object(
                dotenv, "dotenv_values", return_value={"DEEPSEEK_API_KEY": "synthetic"}
            ) as credential_reader,
            patch.object(sys, "argv", denied_argv),
        ):
            wrapper.main()
        denied_folder = task / "tmp/m0-environment/holmes-runs" / case
        denied_result = json.loads((denied_folder / "result-business.json").read_text())
        assert denied_result["initial_evidence_status"] == "unknown"
        assert (
            denied_result["model_http_requests"] == denied_result["tool_queries"] == 0
        )
        assert transport.call_count == credential_reader.call_count == 0
        assert not (denied_folder / "initial-evidence").exists()
        scope_gate_results.append(
            {
                "case": case,
                "model_or_tool_transports": 0,
                "credential_reads": 0,
                "reasons": denied_result["initial_evidence_errors"],
            }
        )
    print(json.dumps({"scope_gate_cases": scope_gate_results, "real_http": 0}))
    print(
        json.dumps(
            {
                "status": "strict_report_only_pass",
                "fake_model_steps": 1,
                "real_http": 0,
                "tool_queries": 0,
                "initial_views": 1,
                "checker_errors": errors,
            }
        )
    )
