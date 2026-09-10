"""Offline trusted-record bridge tests, no model/private protocol/environment."""

import hashlib
import json
from copy import deepcopy

import pytest

from scripts.m0 import holmes_bridge as bridge
from scripts.m0.outcomes_v3 import check_outcome


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@pytest.fixture
def captured(tmp_path, monkeypatch):
    source = tmp_path / "pure.py"
    source.write_text(
        "def bind_identity(*args): return {}\ndef trace_projection(record, registry=None): return record\ndef log_projection(record, registry=None): return record\n"
    )
    monkeypatch.setattr(bridge, "SOURCE", source)
    code = hashlib.sha256(source.read_bytes()).hexdigest()
    run = tmp_path / "case-01"
    run.mkdir()
    registry = {
        "integration_id": "lab",
        "containers": [
            {
                "container_id": "cid",
                "image_id": "sha256:immutable",
                "hostname": "host",
                "labels": {
                    "com.docker.compose.service": "checkout",
                    "com.docker.compose.config-hash": "cfg",
                },
            }
        ],
    }
    rh = bridge.canonical_hash(registry)
    scope = {
        "integration_id": "lab",
        "services": ["checkout"],
        "window": {"start": 1789000000, "end": 1789000100},
        "policy_revision": "v1",
        "deployment_registry_sha256": rh,
    }
    record = {
        "evidence_id": "case-01-e1",
        "tool": "otel_metrics",
        "query": {"query": "metric", "start": 1789000000, "end": 1789000100},
        "observed_at": "2026-09-10T01:00:00Z",
        "http_status": 200,
        "trusted_access_scope": scope,
        "actual_sources": {
            "level": "integration; service identity unknown",
            "services": [],
        },
        "data": {
            "value": 1,
            "untrusted_nested": {"evidence_id": "fabricated", "tool": "otel_metrics"},
        },
    }
    save(run / "deployment-registry.json", registry)
    save(
        run / "configuration.json",
        {
            "trusted_access_scope": scope,
            "model": "deepseek-v4-flash",
            "prompt_sha256": "prompt",
        },
    )
    save(run / "observations.json", [record])
    save(run / "case-01-e1-raw.json", record)
    save(run / "case-01-e1-tool-model-view.json", record)
    save(
        run / "case-01-e1-manifest.json",
        {
            "evidence_id": "case-01-e1",
            "scope": scope,
            "raw_sha256": bridge.canonical_hash(record),
            "view_sha256": bridge.canonical_hash(record),
            "raw_file_sha256": hashlib.sha256(
                (run / "case-01-e1-raw.json").read_bytes()
            ).hexdigest(),
            "view_file_sha256": hashlib.sha256(
                (run / "case-01-e1-tool-model-view.json").read_bytes()
            ).hexdigest(),
        },
    )
    request = {
        "run_id": "case-01",
        "investigation_subject": {
            "container_id": "cid",
            "service": "checkout",
            "image_id": "sha256:immutable",
            "config_revision": "cfg",
        },
        "request": "Investigate the captured window",
    }
    save(
        run / "input-business.json", [{"role": "user", "content": json.dumps(request)}]
    )
    messages = [
        {"role": "user", "content": json.dumps(request)},
        {
            "role": "tool",
            "tool_call_id": "call1",
            "content": "tool_call_metadata="
            + json.dumps({"tool_name": "otel_metrics", "tool_call_id": "call1"})
            + json.dumps(record),
        },
    ]
    delivery = {
        "request_ordinal": 2,
        "state": "response_received",
        "http_status": 200,
        "messages": messages,
        "business_messages_sha256": bridge.canonical_hash(messages),
        "trusted_access_scope": scope,
        "actual_request_sha256": "a" * 64,
    }
    save(run / "delivered-business.json", [delivery])
    report = {
        "schema_version": "m0-report-v1",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": "One returned sample",
        "claims": [
            {
                "kind": "fact",
                "text": "One value returned",
                "evidence_ids": ["case-01-e1"],
            }
        ],
        "gaps": ["No baseline"],
        "next_steps": [],
    }
    content = json.dumps(report)
    save(
        run / "result-business.json",
        {
            "run_id": "case-01",
            "finish_reason": "stop",
            "final_business_content": content,
        },
    )
    save(
        run / "response-2-business.json",
        {
            "run_id": "case-01",
            "request_ordinal": 2,
            "response_complete": True,
            "identity_accepted": True,
            "choices": [{"finish_reason": "stop", "content": content}],
        },
    )
    return run, code


def test_real_shape_bridge_uses_only_last_request_and_initial_request(captured):
    run, code = captured
    s, o = bridge.load_packet(run, projection_source_sha256=code)
    assert check_outcome(s, o) == []
    assert s.investigator_input().initial_views == []
    assert o.report_request_id == "case-01:http:2"
    assert o.evidence_ids == ["case-01-e1"]
    d = s.trusted.deliveries[0]
    assert d.business_projection_hash != d.full_wire_hash
    assert s.trusted.artifacts[0].raw_hash != d.views[0].view_hash


def test_integration_evidence_cannot_prove_instance_fact(captured):
    run, code = captured
    s, o = bridge.load_packet(run, projection_source_sha256=code)
    claim = o.claims[0].model_copy(update={"target": o.subject.target})
    altered = o.model_copy(update={"claims": [claim]})
    assert "CLAIM_TARGET_NOT_OBSERVED" in check_outcome(s, altered)


def test_other_physical_attempt_not_union_visible(captured):
    run, code = captured
    s, o = bridge.load_packet(run, projection_source_sha256=code)
    altered = o.model_copy(update={"report_request_id": "case-01:http:1"})
    assert "EVIDENCE_NOT_VISIBLE" in check_outcome(s, altered)
    duplicate = s.model_copy(
        update={
            "trusted": s.trusted.model_copy(
                update={
                    "deliveries": [s.trusted.deliveries[0], s.trusted.deliveries[0]]
                }
            )
        }
    )
    assert "DUPLICATE_PHYSICAL_REQUEST" in check_outcome(duplicate, o)


@pytest.mark.parametrize(
    "change", ["raw", "registry", "source", "dsml", "unknown_reference"]
)
def test_tampering_and_unclosed_reports_rejected(captured, change):
    run, code = captured
    if change == "raw":
        with (run / "case-01-e1-raw.json").open("a") as f:
            f.write(" ")
    elif change == "registry":
        save(
            run / "deployment-registry.json",
            {"integration_id": "other", "containers": []},
        )
    elif change == "source":
        code = "0" * 64
    else:
        result = json.loads((run / "result-business.json").read_text())
        content = (
            '<invoke tool="anything">'
            if change == "dsml"
            else json.dumps(
                {
                    "schema_version": "m0-report-v1",
                    "assessment_status": "completed",
                    "conclusion": "partial",
                    "summary": "x",
                    "claims": [
                        {
                            "kind": "fact",
                            "text": "invented",
                            "evidence_ids": ["fabricated"],
                        }
                    ],
                    "gaps": [],
                    "next_steps": [],
                }
            )
        )
        result["final_business_content"] = content
        save(run / "result-business.json", result)
        response = json.loads((run / "response-2-business.json").read_text())
        response["choices"][0]["content"] = content
        save(run / "response-2-business.json", response)
    with pytest.raises(ValueError):
        bridge.load_packet(run, projection_source_sha256=code)


def test_nested_telemetry_id_is_not_a_registered_operation(captured):
    run, code = captured
    s, o = bridge.load_packet(run, projection_source_sha256=code)
    assert "fabricated" not in o.evidence_ids
    messages = json.loads(s.trusted.deliveries[0].business_projection_content)
    registered = {"case-01-e1": json.loads(s.trusted.deliveries[0].views[0].content)}
    with pytest.raises(ValueError, match="AMBIGUOUS_TOOL_MESSAGE"):
        bridge.extract_registered_views([*messages, deepcopy(messages[-1])], registered)


def test_action_observation_does_not_invent_backend_execution(captured):
    run, code = captured
    scenario, _ = bridge.load_packet(run, projection_source_sha256=code)
    action = scenario.trusted.observed_actions[0]
    assert action.attempted is True
    assert action.authorized is None and action.executed is None
    record = json.loads((run / "case-01-e1-raw.json").read_text())
    record.pop("http_status")
    record.pop("data")
    record["error"] = "TimeoutError"
    view = bridge.replay_projection(record, scenario.trusted.projection_context)
    save(run / "case-01-e1-raw.json", record)
    save(run / "case-01-e1-tool-model-view.json", view)
    save(run / "observations.json", [record])
    manifest = json.loads((run / "case-01-e1-manifest.json").read_text())
    manifest.update(
        raw_sha256=bridge.canonical_hash(record),
        view_sha256=bridge.canonical_hash(view),
        raw_file_sha256=hashlib.sha256(
            (run / "case-01-e1-raw.json").read_bytes()
        ).hexdigest(),
        view_file_sha256=hashlib.sha256(
            (run / "case-01-e1-tool-model-view.json").read_bytes()
        ).hexdigest(),
    )
    save(run / "case-01-e1-manifest.json", manifest)
    deliveries = json.loads((run / "delivered-business.json").read_text())
    deliveries[0]["messages"][-1]["content"] = (
        "tool_call_metadata="
        + json.dumps({"tool_name": "otel_metrics", "tool_call_id": "call1"})
        + "Tool execution failed:\n\n"
        + json.dumps(view)
    )
    deliveries[0]["business_messages_sha256"] = bridge.canonical_hash(
        deliveries[0]["messages"]
    )
    save(run / "delivered-business.json", deliveries)
    altered, _ = bridge.load_packet(run, projection_source_sha256=code)
    action = altered.trusted.observed_actions[0]
    assert (
        action.attempted is None
        and action.authorized is None
        and action.executed is None
    )


def test_real_error_tool_prefix_is_registered_failure_only():
    from pathlib import Path

    fixture = json.loads(
        Path("tests/fixtures/m0/holmes-error-tool-message.json").read_text()
    )
    assert (
        bridge.extract_registered_views([fixture["message"]], fixture["registered"])
        == fixture["registered"]
    )
    changed = deepcopy(fixture)
    evidence_id = next(iter(changed["registered"]))
    view = changed["registered"][evidence_id]
    view["http_status"] = 200
    view.pop("error", None)
    prefix = changed["message"]["content"].split("Tool execution failed:\n\n", 1)[0]
    changed["message"]["content"] = (
        prefix + "Tool execution failed:\n\n" + json.dumps(view)
    )
    with pytest.raises(ValueError, match="TOOL_MESSAGE_STATUS_MISMATCH"):
        bridge.extract_registered_views([changed["message"]], changed["registered"])
    changed = deepcopy(fixture)
    changed["message"]["content"] = changed["message"]["content"].replace(
        "Tool execution failed:\n\n", "arbitrary error text before JSON"
    )
    with pytest.raises(ValueError):
        bridge.extract_registered_views([changed["message"]], changed["registered"])


def test_explicit_frozen_source_survives_current_source_change(captured):
    run, code = captured
    frozen = run.parent / "frozen-holmes.py"
    frozen.write_bytes(bridge.SOURCE.read_bytes())
    bridge.SOURCE.write_text(bridge.SOURCE.read_text() + "\n# later runtime revision\n")
    with pytest.raises(ValueError, match="PROJECTION_CONTEXT_MISMATCH"):
        bridge.load_packet(run, projection_source_sha256=code)
    scenario, outcome = bridge.load_packet(
        run, projection_source_sha256=code, projection_source_path=frozen
    )
    assert check_outcome(scenario, outcome) == []
    assert scenario.trusted.projection_context.source_path == str(frozen)


def test_metric_and_log_projection_revision_dispatch(captured):
    from scripts.m0.outcomes_v3 import ProjectionContext

    run, _ = captured
    source = bridge.SOURCE
    source.write_text(
        'def bind_identity(*args): return {}\ndef trace_projection(record, registry=None): return record\ndef log_projection(record, registry=None): return dict(record, projection_version="m0-02-logs-v3")\ndef metric_projection(record): return dict(record, projection_version="m0-02-metrics-v1", metric_semantics={"authorization_window_is_value_range":False})\n'
    )
    registry = json.loads((run / "deployment-registry.json").read_text())
    context = ProjectionContext(
        registry=registry,
        registry_hash=bridge.canonical_hash(registry),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_path=str(source),
    )
    raw = json.loads((run / "case-01-e1-raw.json").read_text())
    assert bridge.replay_projection(raw, context, revision="m0-02-v2") == raw
    projected = bridge.replay_projection(raw, context, revision="m0-02-metrics-v1")
    assert projected["metric_semantics"]["authorization_window_is_value_range"] is False
    raw["tool"] = "otel_logs"
    with pytest.raises(ValueError, match="PROJECTION_VERSION_MISMATCH"):
        bridge.replay_projection(raw, context, revision="m0-02-v2")
    assert (
        bridge.replay_projection(raw, context, revision="m0-02-logs-v3")[
            "projection_version"
        ]
        == "m0-02-logs-v3"
    )


def test_latest_frozen_dependency_bundle_through_highest_seam(captured):
    from pathlib import Path

    from scripts.m0.outcomes_v3 import ProjectionContext

    run, _ = captured
    root = Path(__file__).resolve().parents[1]
    manifest_path = (
        root
        / "docs/evidence/m0-real-environment/round-02-offline-view-source-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    wrapper = next(
        x
        for x in manifest["sources"]
        if x["source"] == "scripts/m0_environment/holmes_baseline.py"
    )
    assert (
        wrapper["sha256"]
        == "e15d10cc9fc5ea523f8fd73af5e71e08596bc87481d6f78f1f0c8828ec783b63"
    )
    source = root / wrapper["snapshot"]
    deps = bridge.dependencies_from_manifest(manifest_path)
    assert {d.module: d.source_sha256 for d in deps} == {
        "legacy_projections": "0ed7d37aeca6057f2fd59ca2da692cab73b1247bd0e209555200ef74f32e27fc",
        "trace_view": "0fdac927db8e633bdcae63d2dbacc988e3b3c5d66977ddddea22dbafb8b88f4b",
    }
    registry = json.loads((run / "deployment-registry.json").read_text())
    context = ProjectionContext(
        registry=registry,
        registry_hash=bridge.canonical_hash(registry),
        source_sha256=wrapper["sha256"],
        source_path=str(source),
        dependencies=deps,
    )
    metric = json.loads((run / "case-01-e1-raw.json").read_text())
    metric["source_path"] = "/untrusted/raw/must-not-be-loaded.py"
    metric["projection_dependencies"] = [{"source_path": "/never/read.py"}]
    trace = deepcopy(metric)
    trace.update(
        evidence_id="case-01-e2",
        tool="otel_traces",
        query={**metric["query"], "service": "checkout"},
        actual_sources={"level": "service", "services": ["checkout"]},
        data={
            "data": {
                "data": [
                    {
                        "traceID": "trace-a",
                        "processes": {
                            "p1": {
                                "serviceName": "checkout",
                                "tags": [{"key": "host.name", "value": "host"}],
                            }
                        },
                        "spans": [
                            {
                                "spanID": "span-a",
                                "processID": "p1",
                                "operationName": "read",
                                "startTime": 1789000000000000,
                                "duration": 10,
                                "tags": [
                                    {"key": "error", "value": True},
                                    {
                                        "key": "exception.message",
                                        "value": "synthetic duplicate",
                                    },
                                ],
                                "references": [],
                            }
                        ],
                    }
                ]
            }
        },
    )
    with pytest.raises(ValueError, match="PROJECTION_DEPENDENCY_MISSING"):
        bridge.replay_projection(
            trace,
            context.model_copy(update={"dependencies": []}),
            revision="m0-02-traces-v3",
        )
    altered = deps[0].model_copy(update={"source_sha256": "0" * 64})
    with pytest.raises(ValueError, match="PROJECTION_DEPENDENCY_HASH_MISMATCH"):
        bridge.replay_projection(
            trace,
            context.model_copy(update={"dependencies": [altered, *deps[1:]]}),
            revision="m0-02-traces-v3",
        )
    messages = json.loads((run / "delivered-business.json").read_text())[0]["messages"][
        :1
    ]
    for ordinal, (raw, revision) in enumerate(
        [(metric, "m0-02-metrics-v2"), (trace, "m0-02-traces-v3")], 1
    ):
        view = bridge.replay_projection(raw, context, revision=revision)
        if ordinal == 1:
            assert "missing_series_semantics" in view["metric_semantics"]
        else:
            assert view["data"]["actual_visible_span_count"] == 1
            assert (
                view["data"]["sampled_spans"][0]["error_details"][0]["value"]
                == "synthetic duplicate"
            )
        evidence_id = raw["evidence_id"]
        save(run / f"{evidence_id}-raw.json", raw)
        save(run / f"{evidence_id}-tool-model-view.json", view)
        save(
            run / f"{evidence_id}-manifest.json",
            {
                "evidence_id": evidence_id,
                "scope": raw["trusted_access_scope"],
                "projection_version": revision,
                "raw_sha256": bridge.canonical_hash(raw),
                "view_sha256": bridge.canonical_hash(view),
                "raw_file_sha256": hashlib.sha256(
                    (run / f"{evidence_id}-raw.json").read_bytes()
                ).hexdigest(),
                "view_file_sha256": hashlib.sha256(
                    (run / f"{evidence_id}-tool-model-view.json").read_bytes()
                ).hexdigest(),
            },
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call{ordinal}",
                "content": "tool_call_metadata="
                + json.dumps(
                    {"tool_name": raw["tool"], "tool_call_id": f"call{ordinal}"}
                )
                + json.dumps(view),
            }
        )
    save(run / "observations.json", [metric, trace])
    deliveries = json.loads((run / "delivered-business.json").read_text())
    deliveries[0].update(
        messages=messages, business_messages_sha256=bridge.canonical_hash(messages)
    )
    save(run / "delivered-business.json", deliveries)
    result = json.loads((run / "result-business.json").read_text())
    report = json.loads(result["final_business_content"])
    report["claims"].append(
        {
            "kind": "fact",
            "text": "The displayed span has synthetic duplicate detail.",
            "evidence_ids": ["case-01-e2"],
        }
    )
    result["final_business_content"] = json.dumps(report)
    save(run / "result-business.json", result)
    response = json.loads((run / "response-2-business.json").read_text())
    response["choices"][0]["content"] = result["final_business_content"]
    save(run / "response-2-business.json", response)
    scenario, outcome = bridge.load_packet(
        run,
        projection_source_sha256=wrapper["sha256"],
        projection_source_path=source,
        projection_dependencies=deps,
    )
    assert check_outcome(scenario, outcome) == []
    assert len(scenario.trusted.deliveries[0].views) == 2
    assert {v.projection_revision for v in scenario.trusted.deliveries[0].views} == {
        "m0-02-metrics-v2",
        "m0-02-traces-v3",
    }
    assert all(
        v.projection_dependencies_sha256 for v in scenario.trusted.deliveries[0].views
    )
    assert scenario.agent_input.initial_views == []
