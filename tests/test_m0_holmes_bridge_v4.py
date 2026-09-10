"""Strict bridge integration over isolated files, not new provider experiments."""

import hashlib
import json
from datetime import datetime, timezone

import pytest
from test_m0_holmes_bridge import captured as _captured_fixture
from test_m0_holmes_bridge import save

from scripts.m0 import holmes_bridge as bridge
from scripts.m0 import outcomes_v4 as v4

captured = _captured_fixture


def sync_report_attempt(run, result):
    """Synthetic wire/result/capture describe the same attempt, including failures."""
    response = json.loads((run / "response-2-business.json").read_bytes())
    response["choices"][0].update(
        content=result.get("final_business_content"),
        finish_reason=result.get("finish_reason"),
    )
    save(run / "response-2-business.json", response)
    capture = json.loads((run / "report-capture-business.json").read_bytes())
    capture.update(
        content=result["final_business_content"],
        content_sha256=v4.content_hash(result["final_business_content"]),
    )
    save(run / "report-capture-business.json", capture)


@pytest.fixture
def strict_captured(captured):
    run, code = captured
    registry = json.loads((run / "deployment-registry.json").read_text())
    config = json.loads((run / "configuration.json").read_text())
    config.update(upstream_commit="a" * 40, tool_schema=[])
    scope = config["trusted_access_scope"]
    scope["control_generation"] = 0
    policy = v4.TimePolicy.model_validate_json(
        json.dumps(
            {
                "id": "historical",
                "revision": "p1",
                "integration_id": "lab",
                "interfaces": ["otel_traces"],
                "mode": "historical_window",
                "reference_rule": "response_received_at",
                "window": {
                    "start": datetime.fromtimestamp(
                        scope["window"]["start"], timezone.utc
                    ).isoformat(),
                    "end": datetime.fromtimestamp(
                        scope["window"]["end"], timezone.utc
                    ).isoformat(),
                },
                "all_authorized_targets": True,
                "scope_revision": scope["policy_revision"],
            }
        )
    )
    config.update(
        report_schema_version="m0-report-v2",
        time_policies=[policy.model_dump(mode="json")],
        time_policies_sha256=bridge.canonical_hash([policy.model_dump(mode="json")]),
    )
    save(run / "configuration.json", config)
    save(run / "time-policies.json", config["time_policies"])
    raw = json.loads((run / "case-01-e1-raw.json").read_text())
    raw.update(
        tool="otel_traces",
        query={"service": "checkout", **scope["window"]},
        trusted_access_scope=scope,
        operation_started_at=raw["observed_at"],
        collection_completed_at="2026-09-10T01:00:01Z",
        data={
            "sampled_spans": [
                {
                    "start_us": (scope["window"]["start"] + 10) * 1000000,
                    "duration_us": 1000000,
                }
            ]
        },
    )
    timing = v4.Timing.model_validate_json(
        json.dumps(
            {
                "operation_started_at": raw["operation_started_at"],
                "collection_completed_at": raw["collection_completed_at"],
                "source_start_at": datetime.fromtimestamp(
                    scope["window"]["start"] + 10, timezone.utc
                ).isoformat(),
                "source_end_at": datetime.fromtimestamp(
                    scope["window"]["start"] + 11, timezone.utc
                ).isoformat(),
                "source_time_basis": "event_time",
            }
        )
    )
    context = v4.build_context(
        "case-01",
        {"case-01-e1": raw},
        registry,
        [policy],
        {"case-01-e1": timing},
        allowed_scope=scope,
    )
    save(run / "case-01-e1-raw.json", raw)
    save(run / "case-01-e1-tool-model-view.json", raw)
    save(run / "observations.json", [raw])
    save(
        run / "case-01-e1-manifest.json",
        {
            "evidence_id": "case-01-e1",
            "scope": scope,
            "raw_sha256": bridge.canonical_hash(raw),
            "view_sha256": bridge.canonical_hash(raw),
            "raw_file_sha256": hashlib.sha256(
                (run / "case-01-e1-raw.json").read_bytes()
            ).hexdigest(),
            "view_file_sha256": hashlib.sha256(
                (run / "case-01-e1-tool-model-view.json").read_bytes()
            ).hexdigest(),
        },
    )
    save(
        run / "evidence-timings.json",
        {
            "case-01-e1": {
                "view_hash": bridge.canonical_hash(raw),
                "timing": timing.model_dump(mode="json"),
            }
        },
    )
    user = json.loads((run / "input-business.json").read_text())[0]
    messages = [
        user,
        {
            "role": "tool",
            "tool_call_id": "call1",
            "content": "tool_call_metadata="
            + json.dumps({"tool_name": "otel_traces", "tool_call_id": "call1"})
            + json.dumps(raw),
        },
        {"role": "user", "content": json.dumps(context.model_dump(mode="json"))},
    ]
    delivery = {
        "request_ordinal": 2,
        "request_id": "case-01-http-2",
        "control_generation": 0,
        "state": "response_received",
        "http_status": 200,
        "messages": messages,
        "business_messages_sha256": bridge.canonical_hash(messages),
        "trusted_access_scope": scope,
        "actual_request_sha256": "a" * 64,
        "evidence_context": context.model_dump(mode="json"),
        "evidence_context_sha256": bridge.canonical_hash(
            context.model_dump(mode="json")
        ),
        "dispatch_started_at": "2026-09-10T01:01:00Z",
        "response_received_at": "2026-09-10T01:01:10Z",
    }
    save(run / "delivered-business.json", [delivery])
    report = v4.ModelReportV2.model_validate(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "The complete synthetic summary",
            "claims": [
                {
                    "kind": "fact",
                    "text": "A synthetic event was returned",
                    "evidence_ids": ["case-01-e1"],
                    "target_refs": list(context.target_catalog),
                    "time_scope_ref": "historical",
                }
            ],
            "gaps": [],
            "next_steps": ["Human review of this event"],
        }
    )
    content = json.dumps(report.model_dump(mode="json"))
    save(
        run / "result-business.json",
        {
            "run_id": "case-01",
            "finish_reason": "stop",
            "final_business_content": content,
            "final_report": report.model_dump(mode="json"),
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
    save(
        run / "report-capture-business.json",
        {
            "run_id": "case-01",
            "step_id": "case-01:report-step:2",
            "request_id": "case-01-http-2",
            "control_generation": 0,
            "content": content,
            "content_sha256": v4.content_hash(content),
            "response_received_at": delivery["response_received_at"],
        },
    )
    return run, code


def test_strict_default_preserves_full_report_and_metadata(strict_captured):
    run, code = strict_captured
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert v4.check_outcome(scenario, outcome) == []
    assert outcome.report.summary == "The complete synthetic summary"
    assert outcome.report.next_steps == ["Human review of this event"]
    assert outcome.report_content == scenario.trusted.report_capture.content
    assert outcome.report_request_id == "case-01-http-2"


def test_legacy_requires_explicit_replay_without_fabricating_fields(captured):
    run, code = captured
    with pytest.raises(ValueError, match="LEGACY_REPORT_REQUIRES_EXPLICIT_REPLAY"):
        bridge.load_packet(run, projection_source_sha256=code)
    scenario, outcome = bridge.load_legacy_packet(run, projection_source_sha256=code)
    assert scenario.schema_version == "m0-public-v3"
    assert all(c.target is None for c in outcome.claims)
    audit = bridge.legacy_report_audit(run)
    assert audit["original_report"]["summary"] == "One returned sample"
    assert "next_steps" in audit["original_report"]
    assert audit["strict_scope"] == audit["strict_freshness"] == "unknown"
    assert audit["current_acceptance_pass"] is False


def test_strict_bridge_never_backfills_missing_timing(strict_captured):
    run, code = strict_captured
    save(run / "evidence-timings.json", {})
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert "TIMING_RECORD_UNKNOWN" in v4.check_outcome(scenario, outcome)


def test_output_content_cannot_diverge_between_capture_and_result(strict_captured):
    run, code = strict_captured
    capture = json.loads((run / "report-capture-business.json").read_text())
    capture["content"] = "tampered complete report"
    capture["content_sha256"] = v4.content_hash(capture["content"])
    save(run / "report-capture-business.json", capture)
    with pytest.raises(ValueError, match="REPORT_OUTPUT_BINDING_MISMATCH"):
        bridge.load_packet(run, projection_source_sha256=code)


@pytest.fixture
def initial_captured(strict_captured):
    run, code = strict_captured

    def read(name):
        return json.loads((run / name).read_bytes())

    config = read("configuration.json")
    config.update(phase="report", max_steps=1)
    save(run / "configuration.json", config)
    raw = read("case-01-e1-raw.json")
    manifest = read("case-01-e1-manifest.json")
    manifest.update(query=raw["query"], projection_version="m0-02-v2")
    save(run / "case-01-e1-manifest.json", manifest)
    context = {
        "registry": read("deployment-registry.json"),
        "registry_hash": config["trusted_access_scope"]["deployment_registry_sha256"],
        "source_sha256": code,
        "source_path": str(bridge.SOURCE),
        "dependencies": [],
    }
    entry = {"evidence_id": raw["evidence_id"], "projection_revision": "m0-02-v2"}
    for label, name in [
        ("raw", "case-01-e1-raw.json"),
        ("view", "case-01-e1-tool-model-view.json"),
        ("manifest", "case-01-e1-manifest.json"),
    ]:
        entry[label + "_path"] = name
        entry[label + "_file_sha256"] = hashlib.sha256(
            (run / name).read_bytes()
        ).hexdigest()
    save(
        run / "initial-evidence.json",
        {
            "schema_version": "m0-initial-evidence-v1",
            "projection_context": context,
            "original_projection_context": context,
            "entries": [entry],
        },
    )
    user = read("input-business.json")[0]
    original = user["content"] + "\r\n"
    document = json.loads(user["content"])
    document["business_tool_views"] = [raw]
    user["content"] = json.dumps(document)
    save(run / "input-business.json", [user])
    (run / "question-original.txt").write_bytes(original.encode())
    save(
        run / "input-provenance.json",
        {
            "original_user_content_sha256": v4.content_hash(original),
            "actual_user_content_sha256": v4.content_hash(user["content"]),
        },
    )
    delivery = read("delivered-business.json")[0]
    delivery["messages"] = [user, delivery["messages"][-1]]
    delivery["business_messages_sha256"] = bridge.canonical_hash(delivery["messages"])
    save(run / "delivered-business.json", [delivery])
    (run / "observations.json").unlink()
    return run, code


def test_report_only_initial_raw_and_actual_input_reach_strict_seam(initial_captured):
    run, code = initial_captured
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert v4.check_outcome(scenario, outcome) == []
    assert (
        len(scenario.agent_input.initial_views) == len(scenario.trusted.artifacts) == 1
    )
    assert scenario.trusted.observed_actions == []
    assert scenario.agent_input.original_user_content.endswith("\r\n")
    assert (
        scenario.agent_input.original_user_content_sha256
        != scenario.agent_input.actual_user_content_sha256
    )
    assert outcome.report.claims[0].kind == "fact"


@pytest.mark.parametrize(
    "failure", ["missing", "bad_hash", "duplicate", "invalid_id", "mixed"]
)
def test_unverified_initial_retains_fact_report_without_fabricating_raw(
    initial_captured, failure
):
    run, code = initial_captured
    if failure == "missing":
        (run / "initial-evidence.json").unlink()
    elif failure == "bad_hash":
        value = json.loads((run / "initial-evidence.json").read_bytes())
        value["entries"][0]["raw_file_sha256"] = "b" * 64
        save(run / "initial-evidence.json", value)
    elif failure == "mixed":
        config = json.loads((run / "configuration.json").read_bytes())
        config.update(phase="active", max_steps=4)
        save(run / "configuration.json", config)
        save(run / "observations.json", [])
        code = "b" * 64
    else:
        messages = json.loads((run / "input-business.json").read_bytes())
        doc = json.loads(messages[0]["content"])
        if failure == "duplicate":
            doc["business_tool_views"] *= 2
        else:
            doc["business_tool_views"][0]["evidence_id"] = None
        messages[0]["content"] = json.dumps(doc)
        save(run / "input-business.json", messages)
    result = json.loads((run / "result-business.json").read_bytes())
    result.pop("final_report")  # Qualification rejection may have no parsed duplicate.
    save(run / "result-business.json", result)
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert "UNVERIFIED_INITIAL_EVIDENCE" in v4.check_outcome(scenario, outcome)
    assert scenario.trusted.artifacts == []
    assert scenario.agent_input.initial_views == []
    assert outcome.report.claims[0].kind == "fact"
    assert outcome.report_content == result["final_business_content"]
    assert outcome.report.next_steps == ["Human review of this event"]


def test_import_blocked_before_model_retains_input_without_observations(
    initial_captured,
):
    run, code = initial_captured
    (run / "initial-evidence.json").unlink()
    (run / "delivered-business.json").unlink()
    (run / "report-capture-business.json").unlink()
    (run / "response-2-business.json").unlink()
    save(run / "result-business.json", {"run_id": run.name, "status": "incomplete"})
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert outcome.execution == "blocked" and outcome.handoff and outcome.report is None
    assert scenario.trusted.deliveries == scenario.trusted.artifacts == []
    assert (
        scenario.agent_input.actual_user_content
        and scenario.agent_input.original_user_content
    )
    assert "UNVERIFIED_INITIAL_EVIDENCE" in v4.check_outcome(scenario, outcome)


def test_versions_bind_actual_holmes_and_tools_and_missing_is_unknown(strict_captured):
    run, code = strict_captured
    original, _ = bridge.load_packet(run, projection_source_sha256=code)
    config = json.loads((run / "configuration.json").read_bytes())
    config["upstream_commit"] = "b" * 40
    config["tool_schema"] = [{"name": "different-tool"}]
    save(run / "configuration.json", config)
    changed, _ = bridge.load_packet(run, projection_source_sha256=code)
    assert changed.versions["upstream_commit"] != original.versions["upstream_commit"]
    assert (
        changed.versions["tool_schema_sha256"]
        != original.versions["tool_schema_sha256"]
    )
    for field in ("upstream_commit", "tool_schema"):
        config.pop(field)
    save(run / "configuration.json", config)
    unknown, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert "EXECUTION_VERSION_UNKNOWN" in v4.check_outcome(unknown, outcome)
    assert outcome.report.summary


def test_present_parsed_report_duplicate_must_match_capture(strict_captured):
    run, code = strict_captured
    result = json.loads((run / "result-business.json").read_bytes())
    result["final_report"]["summary"] = "substituted"
    save(run / "result-business.json", result)
    with pytest.raises(ValueError, match="REPORT_PARSED_OBJECT_MISMATCH"):
        bridge.load_packet(run, projection_source_sha256=code)


def test_actual_initial_content_must_be_in_physical_business_payload(initial_captured):
    run, code = initial_captured
    messages = json.loads((run / "input-business.json").read_bytes())
    doc = json.loads(messages[0]["content"])
    doc["request"] = "A different request never physically sent"
    messages[0]["content"] = json.dumps(doc)
    save(run / "input-business.json", messages)
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert "ACTUAL_INITIAL_INPUT_NOT_DELIVERED" in v4.check_outcome(scenario, outcome)


def test_invalid_report_is_retained_as_audit_without_fabricated_report(
    initial_captured,
):
    run, code = initial_captured
    (run / "initial-evidence.json").unlink()
    result = json.loads((run / "result-business.json").read_bytes())
    report = result["final_report"]
    report.pop("summary")
    raw_content = json.dumps(report)
    result["final_business_content"] = raw_content
    result.pop("final_report")
    save(run / "result-business.json", result)
    sync_report_attempt(run, result)
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert outcome.report is None and outcome.report_content == raw_content
    assert outcome.report_content_sha256 == v4.content_hash(raw_content)
    errors = v4.check_outcome(scenario, outcome)
    assert (
        "UNVERIFIED_INITIAL_EVIDENCE" in errors
        and "MISSING_REPORT_OR_HANDOFF" in errors
    )


@pytest.mark.parametrize("failure", ["duplicate", "dsml", "length"])
def test_candidate_parse_reuses_protocol_rules_without_losing_initial_audit(
    initial_captured, failure
):
    run, code = initial_captured
    (run / "initial-evidence.json").unlink()
    result = json.loads((run / "result-business.json").read_bytes())
    result.pop("final_report")
    if failure == "duplicate":
        result["final_business_content"] = result["final_business_content"].replace(
            "{", '{"summary":"discarded duplicate",', 1
        )
    elif failure == "dsml":
        value = json.loads(result["final_business_content"])
        value["summary"] = "<DSML>not a business report</DSML>"
        result["final_business_content"] = json.dumps(value)
    else:
        result["finish_reason"] = "length"
    save(run / "result-business.json", result)
    sync_report_attempt(run, result)
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert outcome.report is None
    assert outcome.report_content == result["final_business_content"]
    assert "UNVERIFIED_INITIAL_EVIDENCE" in v4.check_outcome(scenario, outcome)


@pytest.mark.parametrize("mode", ["import_blocked", "invalid_report", "valid_report"])
def test_cli_reportless_audit_survives_subprocess(initial_captured, mode):
    import subprocess
    import sys

    run, code = initial_captured
    if mode != "valid_report":
        (run / "initial-evidence.json").unlink()
        result = {"run_id": run.name, "status": "incomplete"}
        if mode == "invalid_report":
            result.update(
                finish_reason="stop",
                final_business_content='{"claims": "invalid-sentinel"}',
            )
        save(run / "result-business.json", result)
        if mode == "invalid_report":
            sync_report_attempt(run, result)
        else:
            for name in (
                "delivered-business.json",
                "report-capture-business.json",
                "response-2-business.json",
            ):
                (run / name).unlink()
    output = run / "cli-audit.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.m0.holmes_bridge",
            "--run-dir",
            str(run),
            "--projection-source-sha256",
            code,
            "--projection-source-file",
            str(bridge.SOURCE),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == (0 if mode == "valid_report" else 1)
    assert not process.stderr
    summary = json.loads(process.stdout)
    saved = json.loads(output.read_bytes())
    if mode == "valid_report":
        assert saved["assessment_status"] == "completed"
        assert saved["conclusion"] == "supported"
        assert saved["violations"] == []
    else:
        assert saved["assessment_status"] is None and saved["conclusion"] is None
        assert saved["execution"] == "blocked" and saved["handoff"] is True
        assert saved["report"] is None
        assert "UNVERIFIED_INITIAL_EVIDENCE" in saved["violations"]
        assert saved["agent_input"]["original_user_content"].endswith("\r\n")
        assert saved["agent_input"]["actual_user_content"]
        assert "agent_input" not in summary and "report_content" not in summary
        if mode == "invalid_report":
            assert saved["report_content"] == result["final_business_content"]
            assert saved["report_content_sha256"] == v4.content_hash(
                result["final_business_content"]
            )
            assert "MISSING_REPORT_OR_HANDOFF" in saved["violations"]


@pytest.mark.parametrize("interfaces", [["otel_traces"], ["otel_logs"]])
def test_bridge_preserves_current_interface_restriction(initial_captured, interfaces):
    run, code = initial_captured
    config = json.loads((run / "configuration.json").read_bytes())
    config["trusted_access_scope"]["interfaces"] = interfaces
    save(run / "configuration.json", config)
    deliveries = json.loads((run / "delivered-business.json").read_bytes())
    deliveries[0]["trusted_access_scope"] = config["trusted_access_scope"]
    save(run / "delivered-business.json", deliveries)
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert scenario.trusted.scope.interfaces == interfaces
    if interfaces == ["otel_traces"]:
        assert v4.check_outcome(scenario, outcome) == []
        raw = json.loads((run / "case-01-e1-raw.json").read_bytes())
        window = scenario.trusted.artifacts[0].window
        assert window.start.timestamp() == raw["query"]["start"]
        assert window.end.timestamp() == raw["query"]["end"]
    else:
        assert scenario.trusted.artifacts == []
        assert "UNVERIFIED_INITIAL_EVIDENCE" in v4.check_outcome(scenario, outcome)


@pytest.mark.parametrize("initial_kind", ["none", "verified", "unverified"])
@pytest.mark.parametrize("failure", ["malformed", "empty", "length"])
def test_cli_report_failure_matrix_preserves_common_packet(
    request, initial_kind, failure
):
    import subprocess
    import sys

    run, code = request.getfixturevalue(
        "strict_captured" if initial_kind == "none" else "initial_captured"
    )
    if initial_kind == "unverified":
        (run / "initial-evidence.json").unlink()
    result = json.loads((run / "result-business.json").read_bytes())
    content = {
        "malformed": '{"summary":',
        "empty": "",
        "length": result["final_business_content"],
    }[failure]
    finish = "length" if failure == "length" else "stop"
    result.update(final_business_content=content, finish_reason=finish)
    result.pop("final_report", None)
    save(run / "result-business.json", result)
    response = json.loads((run / "response-2-business.json").read_bytes())
    response["choices"][0].update(content=content, finish_reason=finish)
    save(run / "response-2-business.json", response)
    capture = json.loads((run / "report-capture-business.json").read_bytes())
    capture.update(content=content, content_sha256=v4.content_hash(content))
    save(run / "report-capture-business.json", capture)
    output = run / "failure-cli.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.m0.holmes_bridge",
            "--run-dir",
            str(run),
            "--projection-source-sha256",
            code,
            "--projection-source-file",
            str(bridge.SOURCE),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 1 and not process.stderr
    saved = json.loads(output.read_bytes())
    assert saved["handoff"] is True and saved["execution"] != "completed"
    assert saved["report"] is None and saved["report_content"] == content
    assert saved["report_content_sha256"] == v4.content_hash(content)
    assert saved["violations"] and saved["agent_input"]["actual_user_content"]
    facts = saved["scenario"]["trusted"]
    assert len(facts["artifacts"]) == (0 if initial_kind == "unverified" else 1)
    assert len(facts["observed_actions"]) == (1 if initial_kind == "none" else 0)
    assert len(facts["deliveries"]) == 1
    assert facts["deliveries"][0]["full_wire_hash"] == "a" * 64
    assert saved["outcome"]["report_content"] == content
    summary = json.loads(process.stdout)
    assert all(
        key not in summary
        for key in ["scenario", "outcome", "agent_input", "report_content"]
    )


def test_incomplete_response_retains_attempt_without_committing_it(strict_captured):
    run, code = strict_captured
    save(
        run / "result-business.json",
        {"run_id": run.name, "finish_reason": None, "final_business_content": None},
    )
    (run / "report-capture-business.json").unlink()
    (run / "response-2-business.json").unlink()
    deliveries = json.loads((run / "delivered-business.json").read_bytes())
    deliveries[0].update(state="attempt_outcome_unknown", response_received_at=None)
    save(run / "delivered-business.json", deliveries)
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert outcome.report is None and outcome.report_content is None
    assert outcome.report_content_sha256 is None and outcome.handoff
    assert (
        len(scenario.trusted.artifacts) == len(scenario.trusted.observed_actions) == 1
    )
    assert scenario.trusted.deliveries[0].state == "dispatched"
    assert scenario.trusted.report_capture is None


def test_prior_deliveries_survive_report_failure_with_their_actual_states(
    strict_captured,
):
    from copy import deepcopy

    run, code = strict_captured
    deliveries = json.loads((run / "delivered-business.json").read_bytes())
    prior = deepcopy(deliveries[0])
    prior.update(
        request_ordinal=1,
        request_id="case-01-http-1",
        state="attempt_outcome_unknown",
        response_received_at=None,
    )
    save(run / "delivered-business.json", [prior, *deliveries])
    result = json.loads((run / "result-business.json").read_bytes())
    result.update(final_business_content="", finish_reason="stop")
    result.pop("final_report")
    save(run / "result-business.json", result)
    sync_report_attempt(run, result)
    scenario, outcome = bridge.load_packet(run, projection_source_sha256=code)
    assert [d.state for d in scenario.trusted.deliveries] == [
        "dispatched",
        "response_committed",
    ]
    assert (
        outcome.report_content == ""
        and outcome.report_content_sha256 == v4.content_hash("")
    )
    assert "MISSING_REPORT_OR_HANDOFF" in v4.check_outcome(scenario, outcome)
