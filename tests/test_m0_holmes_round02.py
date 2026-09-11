"""Offline guards at the real wrapper seams; no provider or environment calls."""

import json
import sys
import time

import pytest

from scripts.m0_environment import round02
from scripts.m0_environment.round02 import (
    PROFILE,
    Budget,
    delivered_business,
    envelope_check,
    run_child,
)


@pytest.fixture
def predeadline_budget_clock(monkeypatch):
    real = round02.Budget._clock
    monkeypatch.setattr(
        round02.Budget, "_clock", staticmethod(lambda: round02.PROFILE.deadline - 1)
    )
    yield
    monkeypatch.setattr(round02.Budget, "_clock", real)


def payload():
    return {
        "model": "deepseek-v4-flash",
        "max_tokens": 32768,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "synthetic-private",
                "tool_calls": [],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "x" * 158029},
        ],
    }


def test_actual_wire_boundary_keeps_private_and_separates_units():
    p = payload()
    raw = json.dumps(p).encode()
    original = raw
    envelope_check(raw)
    assert (
        raw == original and p["messages"][0]["reasoning_content"] == "synthetic-private"
    )
    assert PROFILE.input_tokens == 98304
    with pytest.raises(ValueError, match="bytes"):
        envelope_check(raw + b" " * PROFILE.request_bytes)
    p["max_tokens"] = 8192
    with pytest.raises(ValueError, match="output"):
        envelope_check(json.dumps(p).encode())


@pytest.mark.usefixtures("predeadline_budget_clock")
def test_unknown_budget_survives_restart_and_stops_phase(tmp_path):
    path = tmp_path / "ledger.json"
    budget = Budget(path)
    entry = budget.reserve("r1", "report", 123)
    budget.finish(entry, "TimeoutError", None)
    restored = Budget(path)
    assert restored.used("report") == PROFILE.reservation_cny
    with pytest.raises(ValueError, match="phase budget"):
        restored.reserve("r2", "report", 123)
    assert len(json.loads(path.read_text())["attempts"]) == 1


@pytest.mark.usefixtures("predeadline_budget_clock")
def test_complete_usage_releases_only_new_reservation(tmp_path):
    b = Budget(tmp_path / "ledger.json")
    e = b.reserve("r", "normal", 10)
    b.finish(
        e,
        200,
        {
            "prompt_tokens": 1000,
            "completion_tokens": 2000,
            "total_tokens": 3000,
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 900,
        },
    )
    assert b.used("normal") == pytest.approx(0.021)
    assert b.data["previous_allocation_reserved_cny"] == 24
    b.reserve("r", "normal", 10)


@pytest.mark.usefixtures("predeadline_budget_clock")
def test_invalid_usage_keeps_full_reservation(tmp_path):
    b = Budget(tmp_path / "ledger.json")
    e = b.reserve("r", "normal", 10)
    b.finish(e, 200, {"prompt_tokens": -1, "completion_tokens": 0, "total_tokens": -1})
    assert b.used() == PROFILE.reservation_cny


def test_business_delivery_excludes_private():
    p = payload()
    result = delivered_business(p)
    serialized = json.dumps(result)
    assert (
        "synthetic-private" not in serialized and "reasoning_content" not in serialized
    )
    assert result["messages"][0]["role"] == "tool"


def test_child_that_ignores_terminate_is_killed_and_reaped():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        run_child(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)",
            ],
            b"",
            wall_seconds=0.8,
            grace_seconds=0.1,
        )
    assert time.monotonic() - start < 2


@pytest.mark.usefixtures("predeadline_budget_clock")
def test_above_bound_usage_persistently_blocks_other_phase(tmp_path):
    path = tmp_path / "ledger.json"
    b = Budget(path)
    e = b.reserve("r", "report", 10)
    b.finish(
        e,
        200,
        {
            "prompt_tokens": 1048577,
            "completion_tokens": 1,
            "total_tokens": 1048578,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 1048577,
        },
    )
    assert Budget(path).data["blocked_reason"]
    with pytest.raises(ValueError, match="invalid usage"):
        Budget(path).reserve("another", "fault", 10)


def test_full_synthetic_output_response_fits_limit():
    # Adversarial JSON escaping fixture, not proof of an exact provider token/byte ratio.
    response = {
        "model": PROFILE.model,
        "choices": [
            {
                "message": {
                    "reasoning_content": "\U0001f600" * PROFILE.output_tokens,
                    "content": "report",
                },
                "finish_reason": "stop",
            }
        ],
    }
    wire = json.dumps(response, ensure_ascii=True).encode()
    assert len(wire) < PROFILE.response_bytes


def test_saved_delivery_extracts_exact_views_and_hashes():
    view = {
        "evidence_id": "r-e1",
        "tool": "otel_logs",
        "query": {},
        "data": {"body": "full"},
    }
    result = delivered_business(
        {
            "messages": [
                {"role": "user", "content": json.dumps({"business_tool_views": [view]})}
            ]
        }
    )
    assert [e["evidence_id"] for e in result["evidence_views"]] == ["r-e1"]


def test_supervisor_kills_group_after_stubborn_leader(tmp_path):
    from scripts.m0_environment.run_bounded import supervise

    start = time.monotonic()
    result = supervise(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)",
        ],
        timeout=0.8,
        grace=0.1,
    )
    assert result == 124
    assert time.monotonic() - start < 2


def test_model_identity_denial_preserves_response_before_rejection(tmp_path):
    from scripts.m0_environment.round02 import ModelResponseDenied, capture_response

    response = {
        "model": "unexpected-model",
        "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "synthetic business",
                    "reasoning_content": "synthetic private",
                },
            }
        ],
    }
    with pytest.raises(ModelResponseDenied, match="model identity mismatch"):
        capture_response(tmp_path, "new-run", 1, json.dumps(response).encode())
    business = json.loads((tmp_path / "response-1-business.json").read_text())
    assert business["response_model"] == "unexpected-model"
    assert business["identity_accepted"] is False
    assert business["usage"]["prompt_tokens"] == 12
    assert business["choices"][0]["content"] == "synthetic business"
    assert "synthetic private" not in json.dumps(business)
    private = tmp_path / "private-protocol" / "response-1.json"
    assert private.exists() and private.stat().st_mode & 0o777 == 0o600


def test_valid_response_preserves_protocol_without_export(tmp_path):
    from scripts.m0_environment.round02 import capture_response

    response = {
        "model": PROFILE.model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "ok", "reasoning_content": "synthetic private"},
            }
        ],
    }
    returned = capture_response(tmp_path, "r", 1, json.dumps(response).encode())
    assert returned == response
    assert (
        "synthetic private" not in (tmp_path / "response-1-business.json").read_text()
    )


def test_invalid_json_is_preserved_without_business_export(tmp_path):
    from scripts.m0_environment.round02 import ModelResponseDenied, capture_response

    with pytest.raises(ModelResponseDenied, match="JSON invalid"):
        capture_response(tmp_path, "r", 1, b"not-json")
    assert (tmp_path / "private-protocol/response-1.json").exists()
    assert (
        json.loads((tmp_path / "response-1-business.json").read_text())[
            "identity_accepted"
        ]
        is False
    )


def test_actual_trace_source_outside_scope_is_denied():
    from scripts.m0_environment.round02 import source_scope

    record = {
        "tool": "otel_traces",
        "data": {
            "integration_id": "i",
            "data": {"data": [{"processes": {"p": {"serviceName": "forbidden"}}}]},
        },
    }
    with pytest.raises(ValueError, match="returned service outside scope"):
        source_scope(record, {"integration_id": "i", "services": ["checkout"]})


def test_integration_metrics_do_not_fabricate_checkout_identity():
    from scripts.m0_environment.round02 import source_scope

    record = {
        "tool": "otel_metrics",
        "data": {
            "integration_id": "i",
            "data": {"data": {"result": [{"metric": {}, "value": [1, "2"]}]}},
        },
    }
    result = source_scope(
        record,
        {
            "integration_id": "i",
            "services": ["checkout"],
            "metrics_scope": "integration",
        },
    )
    assert result["services"] == [] and "unknown" in result["level"]


def test_raw_and_view_file_hashes_roundtrip(tmp_path):
    import hashlib

    from scripts.m0_environment.round02 import save_observation

    raw = {"evidence_id": "e1", "data": {"unexposed": "full"}}
    view = {"evidence_id": "e1", "data_withheld": True}
    save_observation(tmp_path, raw, view, {})
    manifest = json.loads((tmp_path / "e1-manifest.json").read_text())
    assert (
        manifest["raw_file_sha256"]
        == hashlib.sha256((tmp_path / "e1-raw.json").read_bytes()).hexdigest()
    )
    assert (
        manifest["view_file_sha256"]
        == hashlib.sha256(
            (tmp_path / "e1-tool-model-view.json").read_bytes()
        ).hexdigest()
    )
    assert json.loads((tmp_path / "e1-raw.json").read_text()) == raw


def test_trace_view_keeps_parent_links_and_identity():
    from scripts.m0_environment.holmes_baseline import trace_projection

    record = {
        "evidence_id": "e",
        "data": {
            "data": {
                "data": [
                    {
                        "traceID": "t",
                        "processes": {
                            "p": {
                                "serviceName": "s",
                                "tags": [{"key": "container.id", "value": "cid"}],
                            }
                        },
                        "spans": [
                            {
                                "spanID": "child",
                                "processID": "p",
                                "duration": 1,
                                "references": [
                                    {
                                        "refType": "CHILD_OF",
                                        "traceID": "t",
                                        "spanID": "parent",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    }
    view = trace_projection(record)
    assert (
        view["data"]["sampled_spans"][0]["parent_references"][0]["spanID"] == "parent"
    )
    assert (
        view["data"]["source_identity_table"][0]["attributes"]["container.id"] == "cid"
    )


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"[]", "JSON invalid"),
        (b'{"error":{"message":"synthetic"}}', "provider error envelope"),
        (b"{}", "model identity mismatch"),
    ],
)
def test_unexpected_200_envelopes_preserve_raw_and_fixed_code(tmp_path, raw, code):
    from scripts.m0_environment.round02 import ModelResponseDenied, capture_response

    with pytest.raises(ModelResponseDenied, match=code):
        capture_response(tmp_path, "r", 1, raw)
    assert (tmp_path / "private-protocol/response-1.json").exists()
    assert (
        json.loads((tmp_path / "response-1-business.json").read_text())["http_status"]
        == 200
    )


def test_response_overflow_retains_bounded_private_prefix(tmp_path):
    import base64

    from scripts.m0_environment.round02 import ModelResponseDenied, capture_response
    from scripts.m0_environment.transport_worker import collect_response

    class Response:
        status_code = 200
        headers = {}

        def iter_bytes(self, chunk_size):
            yield b"1234"
            yield b"5678"

    result = collect_response(Response(), 5)
    assert base64.b64decode(result["body"]) == b"12345"
    assert result["complete"] is False
    with pytest.raises(ModelResponseDenied, match="response byte limit exceeded"):
        capture_response(
            tmp_path,
            "r",
            1,
            base64.b64decode(result["body"]),
            complete=False,
            transport_error=result["error"],
        )
    business = json.loads((tmp_path / "response-1-business.json").read_text())
    assert business["response_complete"] is False and "choices" not in business


@pytest.mark.parametrize("finish", [None, [], {}, "length"])
def test_malformed_finish_and_null_content_keep_business_diagnostic(tmp_path, finish):
    from scripts.m0_environment.round02 import capture_response

    raw = json.dumps(
        {
            "model": PROFILE.model,
            "choices": [{"finish_reason": finish, "message": {"content": None}}],
        }
    ).encode()
    capture_response(tmp_path, "r", 1, raw)
    assert (
        json.loads((tmp_path / "response-1-business.json").read_text())["choices"][0][
            "content"
        ]
        is None
    )


@pytest.mark.parametrize(
    "model,accepted",
    [
        ("deepseek-v4-flash", True),
        ("deepseek-flash", True),
        ("deepseek-v4-pro", False),
        ("deepseek-flash-new", False),
        (None, False),
        ({}, False),
    ],
)
def test_exact_reviewed_response_names_only(tmp_path, model, accepted):
    from scripts.m0_environment.round02 import ModelResponseDenied, capture_response

    raw = json.dumps({"model": model, "choices": []}).encode()
    if accepted:
        capture_response(tmp_path, "r", 1, raw)
    else:
        with pytest.raises(ModelResponseDenied):
            capture_response(tmp_path, "r", 1, raw)
    data = json.loads((tmp_path / "response-1-business.json").read_text())
    assert data["identity_accepted"] is accepted
    assert data["requested_model"] == "deepseek-v4-flash"
    assert data["model_metadata_sha256"] == PROFILE.model_metadata_sha256


def test_actual_dsml_content_is_not_a_final_report():
    from pathlib import Path

    from scripts.m0_environment.report_contract import validate_report

    fixture = Path("tests/fixtures/m0_environment/normal01_dsml.json")
    actual = json.loads(fixture.read_text())
    with pytest.raises(ValueError, match="tool protocol"):
        validate_report(
            actual["final_business_content"],
            actual["finish_reason"],
            set(),
            version="m0-report-v1",
        )


def report_example():
    return {
        "schema_version": "m0-report-v1",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": "Observed facts with an unresolved cause.",
        "claims": [
            {"kind": "fact", "text": "A visible fact.", "evidence_ids": ["r-e1"]}
        ],
        "gaps": ["No comparable baseline."],
        "next_steps": [],
    }


def test_final_wire_preserves_all_private_protocol_and_closes_collection():
    from scripts.m0_environment.report_contract import prepare_wire

    original = payload()
    original["messages"][0]["tool_calls"] = [
        {
            "id": "t1",
            "type": "function",
            "function": {"name": "otel_logs", "arguments": "{}"},
        }
    ]
    raw = json.dumps(original, indent=2).encode()
    schemas = [
        {
            "type": "function",
            "function": {"name": "otel_logs", "parameters": {"type": "object"}},
        }
    ]
    assert prepare_wire(raw, schemas, False, version="m0-report-v1") == raw
    final = json.loads(prepare_wire(raw, schemas, True, version="m0-report-v1"))
    assert final["messages"][:-1] == original["messages"]
    assert final["messages"][0]["reasoning_content"] == "synthetic-private"
    assert "tools" not in final and "tool_choice" not in final
    assert final["response_format"] == {"type": "json_object"}
    assert "CLOSED" in final["messages"][-1]["content"]
    assert final["max_tokens"] == original["max_tokens"]
    assert (
        final["thinking"] == original["thinking"]
        and final["reasoning_effort"] == "high"
    )


def test_valid_partial_and_incomplete_reports_are_allowed():
    from scripts.m0_environment.report_contract import validate_report

    report = report_example()
    assert (
        validate_report(json.dumps(report), "stop", {"r-e1"}, version="m0-report-v1")
        == report
    )
    report.update(assessment_status="incomplete", conclusion="inconclusive", claims=[])
    assert (
        validate_report(json.dumps(report), "stop", set(), version="m0-report-v1")
        == report
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(tool_calls=[]),
        lambda r: r["claims"][0].update(evidence_ids=["e1"]),
        lambda r: r.update(assessment_status="incomplete", conclusion="supported"),
        lambda r: r.update(conclusion="supported", claims=[]),
    ],
)
def test_invalid_report_contracts_fail_closed(mutation):
    from scripts.m0_environment.report_contract import validate_report

    report = report_example()
    mutation(report)
    with pytest.raises(ValueError):
        validate_report(json.dumps(report), "stop", {"r-e1"}, version="m0-report-v1")


@pytest.mark.parametrize(
    "content,finish", [("", "stop"), ("{}", "length"), ("not JSON", "stop")]
)
def test_empty_length_and_non_json_never_become_returned(content, finish):
    from scripts.m0_environment.report_contract import validate_report

    with pytest.raises(ValueError):
        validate_report(content, finish, set(), version="m0-report-v1")


def test_actual_counter_view_exposes_time_semantics_without_rewriting_query():
    from pathlib import Path

    from scripts.m0_environment.holmes_baseline import metric_projection

    raw = json.loads(
        Path(
            "tests/fixtures/m0_environment/normal02_metric_counter_raw.json"
        ).read_text()
    )
    original = json.dumps(raw, sort_keys=True)
    view = metric_projection(raw)
    assert view["metric_semantics"]["evaluation_time"] == raw["query"]["end"]
    assert view["metric_semantics"]["authorization_window_is_value_range"] is False
    assert view["query"] == raw["query"] and view["data"] == raw["data"]
    assert json.dumps(raw, sort_keys=True) == original


def test_actual_log_raw_distinguishes_backend_and_visible_records():
    from pathlib import Path

    from scripts.m0_environment.holmes_baseline import log_projection

    raw = json.loads(
        Path("tests/fixtures/m0_environment/normal02_log_raw.json").read_text()
    )
    original = json.dumps(raw, sort_keys=True)
    view = log_projection(raw)
    data = view["data"]
    assert data["backend_returned_hit_count"] == 20
    assert data["model_visible_hit_count"] == len(data["displayed_logs"]) < 20
    assert data["model_visible_hit_count"] + data["omitted_returned_hit_count"] == 20
    assert "returned_hit_count" not in data
    assert len(json.dumps(view, ensure_ascii=False).encode()) <= 14000
    assert json.dumps(raw, sort_keys=True) == original


@pytest.mark.parametrize("steps", [3, 4])
def test_every_active_step_count_requires_trusted_scope(monkeypatch, steps):
    from scripts.m0_environment.holmes_baseline import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "holmes_baseline.py",
            "--phase",
            "normal",
            "--max-steps",
            str(steps),
            "--run-id",
            "scope-denial",
            "--question-file",
            "not-read.json",
            "--preflight-only",
        ],
    )
    with pytest.raises(ValueError, match="trusted scope required"):
        main()


def test_three_steps_cannot_use_report_phase(monkeypatch):
    from scripts.m0_environment.holmes_baseline import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "holmes_baseline.py",
            "--phase",
            "report",
            "--max-steps",
            "3",
            "--run-id",
            "scope-denial",
            "--question-file",
            "not-read.json",
            "--preflight-only",
        ],
    )
    with pytest.raises(ValueError, match="report phase cannot query"):
        main()


def test_historical_wrapper_snapshot_has_exact_executed_hash():
    import hashlib
    from pathlib import Path

    expected = "7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676"
    snapshot = Path("docs/evidence/m0-real-environment/immutable") / (
        expected + ".py.txt"
    )
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == expected


def test_legacy_log_projection_reproduces_original_v2_view():
    from pathlib import Path

    from scripts.m0_environment.holmes_baseline import log_projection, log_projection_v2

    raw = json.loads(
        Path("tests/fixtures/m0_environment/normal02_log_raw.json").read_text()
    )
    legacy = log_projection_v2(raw)
    assert legacy == log_projection(raw, version="m0-02-v2")
    assert legacy["data"]["returned_hit_count"] == 20
    assert "model_visible_hit_count" not in legacy["data"]
    assert log_projection(raw)["data"]["model_visible_hit_count"] == 19
