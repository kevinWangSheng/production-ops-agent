"""r11: current request identities are checked before any judge transport call."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from opspilot.evaluation import canonical_view_hash
from opspilot.evaluation.calibration_flow import (
    _view_identifiers,
    load_verified_calibration,
)
from tests.unit.test_eval_b3_impl import (
    invoke,
    make_input,
    r9_identity_artifact,
)


@pytest.fixture(scope="module")
def verified_calibration(tmp_path_factory):
    root, pack, configs = r9_identity_artifact.__wrapped__(tmp_path_factory)
    return load_verified_calibration(
        root / "calibration.json", pack=pack, config=configs["zai"]
    )


@pytest.mark.parametrize("certified", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "subject_whitespace",
        "subject_unicode",
        "evidence",
        "report",
        "service",
        "operation",
    ],
)
def test_judge_rejects_noncanonical_request_identifiers(
    field, certified, verified_calibration
):
    request, diagnoses, verdict = make_input()
    if field.startswith("subject_"):
        subject = "subject with space" if field == "subject_whitespace" else "ｓubject"
        request = replace(
            request,
            spec=replace(
                request.spec,
                stimulus=replace(request.spec.stimulus, subject_id=subject),
            ),
        )
    elif field == "evidence":
        request = replace(
            request,
            delivered={"e1": replace(request.delivered["e1"], evidence_id="bad id")},
        )
    elif field == "report":
        raw = json.loads(request.report.raw)
        raw["claims"][0]["evidence_ids"] = [" e1 "]
        request = replace(request, report=replace(request.report, raw=json.dumps(raw)))
    else:
        request = replace(
            request,
            answer=replace(
                request.answer,
                injected=replace(
                    request.answer.injected,
                    **{
                        field: "bad service" if field == "service" else "bad  operation"
                    },
                ),
            ),
        )
    observation, transport = invoke(
        request,
        diagnoses,
        verdict,
        calibration=verified_calibration if certified else None,
    )
    assert observation.result.status == "invalid"
    assert observation.error_code == "SCHEMA_INVALID"
    assert observation.result.calibrated is certified
    assert transport.calls == []


@pytest.mark.parametrize("certified", [False, True])
def test_judge_allows_canonical_request_identifiers(certified, verified_calibration):
    observation, transport = invoke(
        calibration=verified_calibration if certified else None
    )
    assert observation.result.status == "ok"
    assert observation.error_code is None
    assert observation.result.calibrated is certified
    assert len(transport.calls) == 1


def with_view(request, view):
    record = request.delivered["e1"]
    return replace(
        request,
        delivered={
            "e1": replace(record, view=view, view_sha256=canonical_view_hash(view))
        },
    )


@pytest.mark.parametrize(
    "container",
    [
        "attributes",
        "tags",
        "labels",
        "process",
        "resource",
        "metric",
        "status_tags",
        "error_details",
    ],
)
@pytest.mark.parametrize("certified", [False, True])
def test_judge_otel_content_keys_are_not_projection_identities(
    container, certified, verified_calibration
):
    request, diagnoses, verdict = make_input()
    content = {
        key: " ordinary attribute value "
        for key in ("service", "operation", "status", "source", "tool")
    }
    # Even names of projection containers inside attributes remain ordinary data.
    content["query"] = {"service": "payments api"}
    value = {"tags": content} if container == "process" else content
    if container == "error_details":
        value = [{"key": "service", "value": content}]
    view = json.loads(json.dumps(request.delivered["e1"].view))
    if container == "status_tags":
        view["content"][0][container].update(value)
    else:
        view["content"][0][container] = value
    observation, transport = invoke(
        with_view(request, view),
        diagnoses,
        verdict,
        calibration=verified_calibration if certified else None,
    )
    assert observation.result.status == "ok"
    assert observation.error_code is None
    assert observation.result.calibrated is certified
    assert len(transport.calls) == 1
    sent = json.loads(json.loads(transport.calls[0]["body"])["messages"][1]["content"])
    assert sent["cited_evidence"][0]["view"] == view


@pytest.mark.parametrize(
    "slot",
    ["metadata", "span", "group", "query", "parent", "backend_ids", "backend_services"],
)
def test_judge_declared_view_slots_still_reject_invalid_identifiers(slot):
    request, diagnoses, verdict = make_input()
    view = json.loads(json.dumps(request.delivered["e1"].view))
    if slot == "metadata":
        view["source"] = "bad source"
    elif slot == "span":
        view["content"][0]["service"] = "bad service"
    elif slot == "group":
        view["span_groups"] = [{"operation": "bad  operation"}]
    elif slot == "query":
        view["query"] = {"service": "bad service"}
    elif slot == "parent":
        view["content"][0]["parent_references"] = [{"spanID": "bad id"}]
    elif slot == "backend_ids":
        view["backend_trace_ids"] = ["bad id"]
    else:
        view["backend_counts_by_service"] = {"bad service": {"span_count": 1}}
    observation, transport = invoke(with_view(request, view), diagnoses, verdict)
    assert observation.result.status == "invalid"
    assert observation.error_code == "SCHEMA_INVALID"
    assert transport.calls == []


@pytest.mark.parametrize("container", ["content", "query"])
@pytest.mark.parametrize("field", ["status", "source", "tool"])
def test_judge_nondeclared_row_and_query_fields_are_content(container, field):
    request, diagnoses, verdict = make_input()
    view = json.loads(json.dumps(request.delivered["e1"].view))
    row = view["content"][0] if container == "content" else view.setdefault("query", {})
    row[field] = " service unavailable HTTP 500 "
    observation, transport = invoke(with_view(request, view), diagnoses, verdict)
    assert observation.result.status == "ok"
    assert observation.error_code is None
    assert len(transport.calls) == 1
    sent = json.loads(json.loads(transport.calls[0]["body"])["messages"][1]["content"])
    assert sent["cited_evidence"][0]["view"] == view


REFERENCE_SLOTS = (
    "evidence_id",
    "operation_id",
    "run_id",
    "subject_id",
    "target_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "time_scope_refs",
)
DECLARED_VIEW_SLOTS = {
    "view": (
        "tool",
        "tool_version",
        "source",
        "registry_revision",
        "tool_registry_revision",
        "projection_revision",
        "projection",
        "trust",
        "status",
        "view_sha256",
        "raw_sha256",
        "source_response_sha256",
        "backend_trace_ids",
        "backend_counts_by_service",
        *REFERENCE_SLOTS,
    ),
    "content": (
        "service",
        "operation",
        "service_version",
        "status_state",
        *REFERENCE_SLOTS,
    ),
    "query": ("service", "operation"),
    "parent_references": ("refType", "traceID", "spanID"),
    "span_groups": ("service", "operation"),
}


@pytest.mark.parametrize(
    "container,field",
    [
        (container, field)
        for container, fields in DECLARED_VIEW_SLOTS.items()
        for field in fields
    ],
)
def test_judge_every_declared_view_slot_rejects_noncanonical_value(container, field):
    request, diagnoses, verdict = make_input()
    view = json.loads(json.dumps(request.delivered["e1"].view))
    if container == "view":
        obj = view
    elif container == "content":
        obj = view["content"][0]
    elif container == "parent_references":
        obj = {}
        view["content"][0][container] = [obj]
    elif container == "span_groups":
        obj = {}
        view[container] = [obj]
    else:
        obj = view.setdefault("query", {})
    bad = " bad  identifier "
    obj[field] = (
        [bad]
        if field in {"time_scope_refs", "backend_trace_ids"}
        else {bad: {"span_count": 1}}
        if field == "backend_counts_by_service"
        else bad
    )
    observation, transport = invoke(with_view(request, view), diagnoses, verdict)
    assert observation.result.status == "invalid"
    assert observation.error_code == "SCHEMA_INVALID"
    assert transport.calls == []


def test_judge_pr_a_all_76_frozen_views_pass_identifier_validation():
    root = (
        Path(__file__).resolve().parents[2] / "docs/evidence/m1-01-alignment-c-effect"
    )
    checked = 0
    for name in (
        "normal-1",
        "normal-2",
        "fault-1",
        "fault-2",
        "pc-fault",
        "cart-fault",
    ):
        records = json.loads((root / name / "ledger.json").read_bytes())["evidence"]
        for record in records:
            _view_identifiers(record["view"])
            checked += 1
    assert checked == 76
