"""PR-A acceptance tests authored only from the r7 public contract.

Authority: docs/tasks/2026-10-13-eval-pr-a-interface.md, plus r6/r7 of
2026-10-12-eval-suite{,-contract}.md. No evaluator implementation is read.
Seven groups follow the contract's section 4. These deterministic fixtures
verify scoring arithmetic and public behavior, not real judge competence.
"""

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from eval_contract_support import (
    CAUSE,
    CHECKS,
    DIMENSIONS,
    EID,
    END,
    OBSERVATION,
    OPERATION,
    SCENARIO_IDS,
    SCOPE,
    START,
    TARGET,
    attempt,
    calibration,
    calibration_inputs,
    capture,
    case,
    claim,
    diagnosis,
    evaluation,
    gate_args,
    judge,
    manifest,
    micros,
    record,
    report_payload,
    rule,
    span,
    trace,
)

from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    AggregateError,
    CalibrationError,
    DimensionValue,
    ManifestError,
    PlannedSlot,
    ReportCapture,
    ScenarioError,
    Window,
    aggregate,
    calibrate,
    canonical_view_hash,
    evaluate,
    investigator_input,
    judge_request,
    load_scenario,
    load_scenarios,
    manifest_hash,
    manifest_unknowns,
    run_gate,
    signature_matches,
    validate_manifest,
)
from opspilot.investigation.reports import ReportV2, parse_report
from opspilot.tools.registry import canonical_hash

SCENARIOS = Path(__file__).resolve().parents[2] / "evals" / "scenarios"


# Group 1: distributed scenarios, schema rejection, answer isolation (P11).


def test_scenarios_six_identities_versions_kinds_and_sorted_loading():
    loaded = load_scenarios(SCENARIOS)
    assert tuple(spec.scenario_id for spec, _ in loaded) == SCENARIO_IDS
    for spec, answer in loaded:
        assert load_scenario(SCENARIOS / spec.scenario_id) == (spec, answer)
        assert spec.scenario_id == answer.scenario_id
        assert spec.version == answer.version and spec.version
        assert answer.expected_output
        if "normal" in spec.scenario_id:
            assert spec.kind == "normal"
            assert answer.injected is None and answer.signature is None
        else:
            assert spec.kind == "fault"
            assert answer.injected is not None and answer.signature is not None
            assert answer.signature.any_of
            assert answer.signature.min_matches >= 1
        assert investigator_input(spec) == {
            "question": spec.stimulus.question,
            "subject_id": spec.stimulus.subject_id,
            "target_id": spec.stimulus.target_id,
        }


def copy_scenario(tmp_path, scenario_id="eval-cart-failure"):
    destination = tmp_path / scenario_id
    destination.mkdir()
    for filename in ("scenario.json", "answer.json"):
        (destination / filename).write_bytes(
            (SCENARIOS / scenario_id / filename).read_bytes()
        )
    return destination


@pytest.mark.parametrize("field", ["expected_output", "injected"])
def test_scenarios_answer_changes_cannot_change_investigator_input(tmp_path, field):
    directory = copy_scenario(tmp_path)
    original, _ = load_scenario(directory)
    path = directory / "answer.json"
    payload = json.loads(path.read_text())
    sentinel = "EVALUATOR_ONLY_SENTINEL"
    if field == "expected_output":
        payload[field] = [sentinel]
    else:
        payload[field] = {
            **payload[field],
            "service": sentinel,
            "operation": sentinel,
            "flag": sentinel,
            "variant": sentinel,
        }
    path.write_text(json.dumps(payload))
    changed, answer = load_scenario(directory)
    assert sentinel in json.dumps(asdict(answer))
    assert investigator_input(changed) == investigator_input(original)
    assert sentinel not in json.dumps(investigator_input(changed))


def test_investigator_input_accepts_only_spec_and_omits_answer_and_id():
    data = case()
    visible = investigator_input(data["spec"])
    assert set(visible) == {"question", "subject_id", "target_id"}
    assert data["spec"].scenario_id not in json.dumps(visible)
    with pytest.raises(TypeError):
        investigator_input(data["spec"], data["answer"])


@pytest.mark.parametrize(
    "mutation",
    [
        "scenario_id",
        "version",
        "missing_injected",
        "missing_signature",
        "null_injected",
        "null_signature",
        "empty_any_of",
        "zero_min_matches",
        "negative_min_matches",
        "empty_expected",
    ],
)
def test_scenarios_reject_invalid_answer_contract(tmp_path, mutation):
    directory = copy_scenario(tmp_path)
    path = directory / "answer.json"
    payload = json.loads(path.read_text())
    if mutation in {"scenario_id", "version"}:
        payload[mutation] = "mismatched"
    elif mutation.startswith("missing_"):
        payload.pop(mutation.removeprefix("missing_"))
    elif mutation.startswith("null_"):
        payload[mutation.removeprefix("null_")] = None
    elif mutation == "empty_any_of":
        payload["signature"]["any_of"] = []
    elif mutation.endswith("min_matches"):
        payload["signature"]["min_matches"] = 0 if mutation.startswith("zero") else -1
    else:
        payload["expected_output"] = []
    path.write_text(json.dumps(payload))
    with pytest.raises(ScenarioError):
        load_scenario(directory)


@pytest.mark.parametrize("field", ["injected", "signature"])
def test_scenarios_normal_rejects_fault_answer_fields(tmp_path, field):
    directory = copy_scenario(tmp_path, "eval-normal-1")
    path = directory / "answer.json"
    payload = json.loads(path.read_text())
    fault_answer = json.loads(
        (SCENARIOS / "eval-cart-failure" / "answer.json").read_text()
    )
    payload[field] = fault_answer[field]
    path.write_text(json.dumps(payload))
    with pytest.raises(ScenarioError):
        load_scenario(directory)


@pytest.mark.parametrize(
    "field", ["expected_output", "injected", "signature", "private_hint"]
)
def test_scenarios_reject_nonwhitelisted_model_visible_fields(tmp_path, field):
    directory = copy_scenario(tmp_path)
    path = directory / "scenario.json"
    payload = json.loads(path.read_text())
    # AMBIGUOUS: interface:89 gives a whitelist but no JSON nesting/schema.
    # Use the distributed valid document and its stimulus container, if present;
    # do not prescribe a new wire format for the implementer.
    stimulus = payload.get("stimulus", payload)
    stimulus[field] = "EVALUATOR_ONLY_SENTINEL"
    path.write_text(json.dumps(payload))
    with pytest.raises(ScenarioError):
        load_scenario(directory)


def test_public_dtos_are_frozen():
    data = case()
    objects = [
        data["spec"],
        data["spec"].stimulus,
        data["answer"],
        data["answer"].injected,
        rule(),
        rule().any_of[0],
        record(),
        Window(START, END),
        data["report"],
        data["attempt"],
        trace(),
        diagnosis(),
        judge(),
        manifest(),
        DimensionValue("none", None, "offline"),
        PlannedSlot("s", "v1", "live", 1),
        calibration(),
        evaluation(),
        evaluation().gate,
    ]
    for obj in objects:
        name = fields(obj)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(obj, name, getattr(obj, name))


# Group 2: five gate checks, binding and exact visible span signatures (P8).


@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize("conclusion", ["supported", "partial", "inconclusive"])
def test_gate_accepts_completed_reports_and_has_exact_five_checks(fault, conclusion):
    data = case(fault=fault)
    data["report"] = capture(fault=fault, conclusion=conclusion)
    parsed, error = parse_report(data["report"].raw, finish_reason="stop")
    assert isinstance(parsed, ReportV2) and error == ""
    result = run_gate(**gate_args(data))
    assert result.passed is True
    assert result.checks == dict.fromkeys(CHECKS, True)
    assert result.reasons == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"final_state": state}
        for state in ("failed", "blocked", "budget_exhausted", "cancelled", "paused")
    ]
    + [
        {"report_available": False},
        {"decision": "handoff"},
        {"human_interaction": "handoff"},
        {"handoff_reasons": ("needs_operator",)},
    ],
)
def test_gate_execution_requires_every_completion_condition(changes):
    data = case()
    data["outcome"] = replace(data["outcome"], **changes)
    result = run_gate(**gate_args(data))
    assert not result.passed and not result.checks["execution"]
    assert "EXECUTION_NOT_COMPLETED" in result.reasons


def test_gate_missing_outcome_fails_execution():
    data = case()
    data["outcome"] = None
    result = run_gate(**gate_args(data))
    assert not result.passed and not result.checks["execution"]
    assert "EXECUTION_NOT_COMPLETED" in result.reasons


@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize(
    "bad_report",
    [
        None,
        ReportCapture(None, "stop"),
        ReportCapture("", "stop"),
        ReportCapture("not-json", "stop"),
        ReportCapture("{}", "stop"),
        ReportCapture(capture().raw, "length"),
        ReportCapture(capture().raw, "tool_calls"),
    ],
)
def test_gate_invalid_report_disables_citations_and_signature_without_spurious_reasons(
    fault, bad_report
):
    data = case(fault=fault)
    data["report"] = bad_report
    result = run_gate(**gate_args(data))
    assert not result.passed
    assert result.checks == {
        "execution": True,
        "report": False,
        "permissions": True,
        "citations": False,
        "signature": False,
    }
    # AMBIGUOUS: interface:111 says normal signature is always true; :113
    # explicitly says parsing failure sets it false. Use the explicit exception.
    assert result.reasons == ("REPORT_INVALID",)


def test_gate_incomplete_report_is_parseable_but_rejected():
    data = case(fault=False)
    data["report"] = capture(
        fault=False, assessment_status="incomplete", conclusion="inconclusive"
    )
    assert parse_report(data["report"].raw, finish_reason="stop")[0] is not None
    result = run_gate(**gate_args(data))
    assert not result.passed and not result.checks["report"]
    assert result.reasons == ("REPORT_INCOMPLETE",)


def test_gate_normal_completed_without_claims_or_evidence_can_pass():
    data = case(fault=False)
    data["report"] = capture(fault=False, conclusion="inconclusive", claims=[])
    data["delivered"] = {}
    data["outcome"] = replace(data["outcome"], evidence_ids=(), actions=())
    result = run_gate(**gate_args(data))
    assert result.checks == dict.fromkeys(CHECKS, True)
    assert result.passed and evaluate(**data).overall


@pytest.mark.parametrize("kind", ["fact", "counter_evidence", "rejected_hypothesis"])
@pytest.mark.parametrize(
    "changes,code",
    [
        ({"delivered": False}, "CITATION_NOT_DELIVERED"),
        ({"view_sha256": "0" * 64}, "VIEW_HASH_MISMATCH"),
        ({"status": "error"}, "CITATION_NOT_FACT_EVIDENCE"),
        ({"status": "no_data"}, "CITATION_NOT_FACT_EVIDENCE"),
        ({"status": "timeout"}, "CITATION_NOT_FACT_EVIDENCE"),
        ({"status": "denied"}, "CITATION_NOT_FACT_EVIDENCE"),
        ({"citable_as_fact": False}, "CITATION_NOT_FACT_EVIDENCE"),
        ({"target_id": "other-target"}, "TARGET_MISMATCH"),
        ({"time_scope_refs": frozenset({"other-scope"})}, "TIME_SCOPE_MISMATCH"),
        (
            {"window": Window(START - timedelta(microseconds=1), END)},
            "WINDOW_OUTSIDE_AUTHORIZED",
        ),
        (
            {"window": Window(START, END + timedelta(microseconds=1))},
            "WINDOW_OUTSIDE_AUTHORIZED",
        ),
    ],
)
def test_gate_factlike_citations_reject_each_binding_failure(kind, changes, code):
    data = case()
    data["report"] = capture(claims=[claim(kind=kind)])
    data["delivered"][EID] = record(**changes)
    result = run_gate(**gate_args(data))
    assert not result.passed and not result.checks["citations"]
    assert code in result.reasons


@pytest.mark.parametrize(
    "kind",
    ["fact", "counter_evidence", "rejected_hypothesis", "hypothesis", "recommendation"],
)
@pytest.mark.parametrize("bad_binding", ["unknown", "undelivered", "hash"])
def test_gate_delivery_and_hash_required_for_every_claim_kind(kind, bad_binding):
    data = case(fault=False)
    data["report"] = capture(fault=False, claims=[claim(OBSERVATION, kind=kind)])
    if bad_binding == "unknown":
        data["delivered"] = {}
    else:
        changes = (
            {"delivered": False}
            if bad_binding == "undelivered"
            else {"view_sha256": "bad"}
        )
        data["delivered"][EID] = record(**changes)
    result = run_gate(**gate_args(data))
    assert not result.checks["citations"] and not result.passed
    assert (
        "VIEW_HASH_MISMATCH" if bad_binding == "hash" else "CITATION_NOT_DELIVERED"
    ) in result.reasons


@pytest.mark.parametrize("kind", ["hypothesis", "recommendation"])
def test_gate_nonfact_claim_may_cite_delivered_nonfact_view(kind):
    data = case(fault=False)
    data["report"] = capture(fault=False, claims=[claim(OBSERVATION, kind=kind)])
    data["delivered"][EID] = record(status="no_data", citable_as_fact=False)
    assert run_gate(**gate_args(data)).passed


def test_gate_requires_scope_in_authorization_and_all_target_refs_to_match():
    data = case()
    data["authorized"] = {"other-scope": Window(START, END)}
    result = run_gate(**gate_args(data))
    assert "TIME_SCOPE_MISMATCH" in result.reasons and not result.passed
    data = case()
    data["report"] = capture(claims=[claim(target_refs=[TARGET, "other-target"])])
    result = run_gate(**gate_args(data))
    assert "TARGET_MISMATCH" in result.reasons and not result.passed


@pytest.mark.parametrize("same_claim", [False, True])
def test_gate_checks_every_citation_not_only_first_good_evidence(same_claim):
    data = case()
    bad = record(evidence_id="ev-second", view_sha256="bad")
    data["delivered"][bad.evidence_id] = bad
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID, bad.evidence_id))
    claims = (
        [claim(evidence_ids=[EID, bad.evidence_id])]
        if same_claim
        else [claim(), claim(evidence_ids=[bad.evidence_id])]
    )
    data["report"] = capture(claims=claims)
    result = run_gate(**gate_args(data))
    assert not result.passed and not result.checks["citations"]
    assert "VIEW_HASH_MISMATCH" in result.reasons


@pytest.mark.parametrize(
    "window", [Window(START, END), Window(START, START), Window(END, END)]
)
def test_gate_authorized_window_is_closed_including_zero_width(window):
    data = case()
    data["delivered"][EID] = record(
        window=window, rows=[span(start_us=micros(window.start))]
    )
    assert run_gate(**gate_args(data)).passed


@pytest.mark.parametrize("fault", [False, True])
def test_gate_signature_must_be_in_report_citations_and_is_fault_only(fault):
    data = case(fault=fault)
    other = record(evidence_id="ev-no-error", rows=[span(status_tags={})])
    data["delivered"][other.evidence_id] = other
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID, other.evidence_id))
    data["report"] = capture(
        fault=fault, claims=[claim(evidence_ids=[other.evidence_id])]
    )
    result = run_gate(**gate_args(data))
    assert result.checks["citations"]
    assert result.checks["signature"] is (not fault)
    assert result.passed is (not fault)
    assert result.reasons == (("SIGNATURE_ABSENT",) if fault else ())


def test_gate_signature_minimum_must_be_met_by_at_least_one_record():
    data = case()
    data["answer"] = replace(data["answer"], signature=rule(min_matches=2))
    data["delivered"]["ev-second"] = record(
        evidence_id="ev-second", rows=[span(span_id="span-second")]
    )
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID, "ev-second"))
    data["report"] = capture(claims=[claim(evidence_ids=[EID, "ev-second"])])
    result = run_gate(**gate_args(data))
    assert result.checks["citations"] and not result.checks["signature"]
    assert result.reasons == ("SIGNATURE_ABSENT",) and not result.passed


def test_canonical_view_hash_matches_product_for_unicode_nested_and_reordered_views():
    view = {
        "tool": "traces_search",
        "content": [{"text": "错误", "n": 3}],
        "extra": {"b": 2, "a": 1},
    }
    reordered = {
        "extra": {"a": 1, "b": 2},
        "content": [{"n": 3, "text": "错误"}],
        "tool": "traces_search",
    }
    assert canonical_view_hash(view) == canonical_hash(view)
    assert canonical_view_hash(reordered) == canonical_hash(view)
    assert canonical_view_hash({**view, "tool": "logs_search"}) != canonical_hash(view)


@pytest.mark.parametrize("service", ["cart", "checkout"])
def test_signature_accepts_exact_server_or_client_span(service):
    assert signature_matches(rule(), record(rows=[span(service=service)])) == (
        "span-cart",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "EmptyCart"},
        {"operation": "OtherService/EmptyCart"},
        {"operation": "oteldemo.CartService/GetCart"},
        {"operation": ""},
        {"service": "checkout", "operation": "oteldemo.CheckoutService/PlaceOrder"},
        {"service": "payment"},
        {"service": "Cart"},
        {"operation": OPERATION.lower()},
        {"operation_truncated": True},
        {"status_tags": {}},
    ],
)
def test_signature_rejects_suffix_service_only_checkout_error_case_and_truncation(
    changes,
):
    assert signature_matches(rule(), record(rows=[span(**changes)])) == ()


@pytest.mark.parametrize("missing", ["service", "operation", "start_us", "status_tags"])
def test_signature_missing_required_visible_fields_cannot_match(missing):
    row = span()
    row.pop(missing)
    assert signature_matches(rule(), record(rows=[row])) == ()


@pytest.mark.parametrize(
    "tags,expected",
    [
        ({"error": True}, True),
        ({"error": False}, False),
        ({"error": "true"}, False),
        ({"otel.status_code": "ERROR"}, True),
        ({"otel.status_code": "error"}, False),
        ({"otel.status_code": "OK"}, False),
        ({"rpc.grpc.status_code": 9}, True),
        ({"rpc.grpc.status_code": 0}, False),
        ({"http.status_code": 500}, True),
        ({"http.status_code": "503"}, True),
        ({"http.status_code": 404}, False),
        ({"http.status_code": 200}, False),
        ({}, False),
        (
            {"error": False, "otel.status_code": "ERROR", "rpc.grpc.status_code": 0},
            True,
        ),
    ],
)
@pytest.mark.parametrize("row_error_flag", [False, True])
def test_signature_recomputes_error_from_tags_not_cached_row_flag(
    tags, expected, row_error_flag
):
    row = span(status_tags=tags, error_by_visible_tags=row_error_flag)
    assert signature_matches(rule(), record(rows=[row])) == (
        ("span-cart",) if expected else ()
    )


@pytest.mark.parametrize(
    "when,expected",
    [
        (START - timedelta(microseconds=1), False),
        (START, True),
        (END, True),
        (END + timedelta(microseconds=1), False),
    ],
)
def test_signature_window_start_time_endpoints_have_no_tolerance(when, expected):
    actual = signature_matches(rule(), record(rows=[span(start_us=micros(when))]))
    assert actual == (("span-cart",) if expected else ())


def test_signature_requires_query_window_and_traces_tool_and_minimum_matches():
    assert signature_matches(rule(), record(window=None)) == ()
    assert signature_matches(rule(), record(tool="metrics_query")) == ()
    assert signature_matches(rule(), record(rows=[])) == ()
    assert signature_matches(rule(min_matches=2), record()) == ()
    rows = [span(span_id="span-a"), span(span_id="span-b", service="checkout")]
    assert set(signature_matches(rule(min_matches=2), record(rows=rows))) == {
        "span-a",
        "span-b",
    }
    assert signature_matches(rule(min_matches=3), record(rows=rows)) == ()


def test_signature_counts_only_matching_rows_toward_minimum():
    rows = [span(span_id="match"), span(span_id="unrelated", operation="HTTP POST")]
    assert signature_matches(rule(min_matches=2), record(rows=rows)) == ()


# Group 3: causal diagnosis recomputation, per-diagnosis citation ownership.


@pytest.mark.parametrize("fault", [False, True])
def test_evaluate_success_exposes_signature_and_all_pass_flags(fault):
    result = evaluate(**case(fault=fault))
    assert result.gate.passed and result.deterministic_pass
    assert result.judge_status == "ok"
    assert result.diagnosis_pass is True and result.judge_pass and result.overall
    assert result.failure_class is None and result.failure_reasons == ()
    assert result.evidence_status == "complete"
    if fault:
        assert result.signature_matches[EID] == ("span-cart",)


@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize("factual", [False, True])
def test_diagnosis_normal_requires_zero_and_fault_requires_nonempty_causal_set(
    fault, factual
):
    data = case(fault=fault)
    data["report"] = capture()
    expected = factual if fault else not factual
    data["judge"] = judge(
        diagnoses=(diagnosis(factual_causal=factual),), verdict=expected
    )
    result = evaluate(**data)
    assert result.judge_status == "ok"
    assert result.diagnosis_pass is expected
    assert result.judge_pass is expected and result.overall is expected
    assert result.failure_class == (None if expected else "diagnosis")
    assert ("DIAGNOSIS_FAILED" in result.failure_reasons) is (not expected)


def test_diagnosis_fault_empty_set_does_not_pass_vacuously():
    data = case()
    data["judge"] = judge(diagnoses=(), verdict=False)
    result = evaluate(**data)
    assert result.gate.passed and result.deterministic_pass
    assert result.judge_status == "ok" and result.diagnosis_pass is False
    assert not result.overall and result.failure_class == "diagnosis"


@pytest.mark.parametrize(
    "changes",
    [
        {"service": "valkey"},
        {"operation": "GetCart"},
        {"service": None},
        {"operation": None},
        {"supported": False},
        {"cited_evidence_ids": ()},
    ],
)
def test_diagnosis_each_causal_item_must_identify_injection_and_have_own_support(
    changes,
):
    data = case()
    data["judge"] = judge(diagnoses=(diagnosis(**changes),), verdict=False)
    result = evaluate(**data)
    assert result.gate.passed
    assert result.judge_status == "ok" and result.diagnosis_pass is False
    assert not result.judge_pass and not result.overall
    assert result.failure_class == "diagnosis"


@pytest.mark.parametrize(
    "service,operation",
    [
        ("cart", OPERATION),
        ("CART", OPERATION.upper()),
        ("CartService", "emptycart"),
        ("CARTSERVICE", "EMPTYCART"),
    ],
)
def test_diagnosis_aliases_are_case_insensitive_but_span_matching_is_exact(
    service, operation
):
    data = case()
    data["judge"] = judge(diagnoses=(diagnosis(service=service, operation=operation),))
    result = evaluate(**data)
    assert result.diagnosis_pass is True and result.overall


def test_diagnosis_extra_wrong_attribution_fails_entire_fault_report():
    data = case()
    extra = "valkey outage caused all requests to fail."
    data["report"] = capture(claims=[claim(), claim(extra)])
    data["judge"] = judge(
        diagnoses=(
            diagnosis(),
            diagnosis(location="claims[1]", quote=extra, service="valkey"),
        ),
        verdict=False,
    )
    result = evaluate(**data)
    assert result.gate.passed and result.judge_status == "ok"
    assert result.diagnosis_pass is False and not result.overall
    assert result.failure_reasons == ("DIAGNOSIS_FAILED",)


def test_diagnosis_pure_hypothesis_does_not_invalidate_supported_fault():
    data = case()
    hypothesis = "Payment might also be affected; this is unconfirmed."
    data["report"] = capture(
        claims=[claim(), claim(hypothesis, kind="hypothesis", evidence_ids=[])]
    )
    data["judge"] = judge(
        diagnoses=(
            diagnosis(),
            diagnosis(
                location="claims[1]",
                quote=hypothesis,
                factual_causal=False,
                service="payment",
                operation=None,
                cited_evidence_ids=(),
                supported=False,
            ),
        )
    )
    assert evaluate(**data).overall


def test_diagnosis_gate_signature_cannot_be_borrowed_from_another_claim():
    data = case()
    other_id = "ev-no-signature"
    data["delivered"][other_id] = record(
        evidence_id=other_id, rows=[span(status_tags={})]
    )
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID, other_id))
    data["report"] = capture(
        claims=[
            claim(OBSERVATION),
            claim(CAUSE, evidence_ids=[other_id]),
        ]
    )
    data["judge"] = judge(
        diagnoses=(
            diagnosis(
                location="claims[1]",
                cited_evidence_ids=(other_id,),
            ),
        ),
        verdict=False,
    )
    result = evaluate(**data)
    assert result.gate.passed and result.signature_matches[EID] == ("span-cart",)
    assert result.judge_status == "ok" and result.diagnosis_pass is False
    assert not result.overall and result.failure_class == "diagnosis"


@pytest.mark.parametrize("location", ["summary", "gaps[0]", "next_steps[0]"])
def test_diagnosis_nonclaim_location_requires_explicit_claim_link_for_support(location):
    data = case()
    data["report"] = capture(gaps=[CAUSE], next_steps=[CAUSE])
    data["judge"] = judge(diagnoses=(diagnosis(location=location, linked_claim=0),))
    assert evaluate(**data).overall
    data["judge"] = judge(
        diagnoses=(
            diagnosis(
                location=location,
                linked_claim=None,
                cited_evidence_ids=(),
            ),
        ),
        verdict=False,
    )
    result = evaluate(**data)
    assert result.judge_status == "ok" and result.diagnosis_pass is False
    assert not result.overall
    # AMBIGUOUS: P6 (contract:137) requires a synonymous linked claim, while
    # interface:124 validates only its index and citation subset. The positive
    # fixture uses identical text; no invented deterministic synonym oracle.


@pytest.mark.parametrize(
    "location", ["summary", "claims[0]", "gaps[0]", "next_steps[0]"]
)
def test_diagnosis_normal_causal_attribution_anywhere_fails(location):
    data = case(fault=False)
    data["report"] = capture(gaps=[CAUSE], next_steps=[CAUSE])
    data["judge"] = judge(
        diagnoses=(
            diagnosis(
                location=location,
                linked_claim=None if location == "claims[0]" else 0,
            ),
        ),
        verdict=False,
    )
    result = evaluate(**data)
    assert result.judge_status == "ok" and result.diagnosis_pass is False
    assert result.failure_class == "diagnosis" and not result.overall


def test_diagnosis_normal_rejects_cause_outside_fault_catalog():
    data = case(fault=False)
    text = "An unrelated DNS outage caused checkout failures."
    data["report"] = capture(fault=False, summary=text, claims=[claim(text)])
    data["judge"] = judge(
        diagnoses=(
            diagnosis(
                quote=text,
                service="external-dns",
                operation="lookup",
            ),
        ),
        verdict=False,
    )
    result = evaluate(**data)
    assert result.gate.passed and result.judge_status == "ok"
    assert result.diagnosis_pass is False and not result.overall


# Group 4: permissions, judge transport/output states, AND truth table, request.


@pytest.mark.parametrize(
    "permissions,expected",
    [
        (("read_only",), True),
        (("read_only", "human_control"), True),
        (("human_control", "read_only"), True),
        ((), False),
        (("human_control",), False),
        (("read_only", "write"), False),
        (("read_only", "unknown"), False),
        (("write",), False),
    ],
)
def test_gate_permissions_require_read_only_and_allow_only_human_control(
    permissions, expected
):
    data = case()
    data["outcome"] = replace(data["outcome"], permissions=permissions)
    result = run_gate(**gate_args(data))
    assert result.checks["permissions"] is expected and result.passed is expected
    assert ("PERMISSION_VIOLATION" in result.reasons) is (not expected)
    assert data["outcome"].permissions == permissions


@pytest.mark.parametrize("status", ["error", "timeout", "invalid", "oversize"])
def test_judge_non_ok_statuses_fail_closed_with_exact_reason(status):
    data = case()
    data["judge"] = judge(status=status, diagnoses=(), verdict=None)
    result = evaluate(**data)
    assert result.deterministic_pass and not result.judge_pass and not result.overall
    assert result.judge_status == status and result.diagnosis_pass is None
    assert result.failure_class == "judge_error"
    assert result.failure_reasons == (f"JUDGE_{status.upper()}",)
    # AMBIGUOUS: section-4 test group 4 mentions invalid judge JSON, but the
    # public seam accepts JudgeResult, not raw JSON. Test status="invalid";
    # no undocumented judge response parser is imported or fabricated.


def test_judge_uncalibrated_is_not_quality_pass():
    data = case()
    data["judge"] = judge(calibrated=False)
    result = evaluate(**data)
    assert result.judge_status == "uncalibrated" and result.diagnosis_pass is None
    assert result.failure_class == "judge_error"
    assert result.failure_reasons == ("JUDGE_UNCALIBRATED",)
    assert result.deterministic_pass and not result.judge_pass and not result.overall


@pytest.mark.parametrize("gate_pass", [False, True])
def test_judge_absent_is_skipped_only_when_gate_failed(gate_pass):
    data = case()
    data["judge"] = None
    if not gate_pass:
        data["outcome"] = replace(data["outcome"], permissions=("write",))
    result = evaluate(**data)
    assert result.judge_status == ("not_run" if gate_pass else "skipped")
    assert (
        result.diagnosis_pass is None and not result.judge_pass and not result.overall
    )
    assert result.failure_class == (
        "judge_error" if gate_pass else "deterministic_gate"
    )
    if gate_pass:
        assert result.failure_reasons == ("JUDGE_NOT_RUN",)


@pytest.mark.parametrize(
    "location",
    [
        "",
        "body",
        "claims",
        "claims[-1]",
        "claims[1]",
        "summary[0]",
        "gaps[1]",
        "next_steps[1]",
    ],
)
def test_judge_output_rejects_invalid_or_out_of_range_location(location):
    data = case()
    data["judge"] = judge(diagnoses=(diagnosis(location=location),))
    result = evaluate(**data)
    assert result.judge_status == "invalid" and result.diagnosis_pass is None
    assert "JUDGE_BAD_LOCATION" in result.failure_reasons
    assert result.failure_class == "judge_error" and not result.overall


@pytest.mark.parametrize("quote", ["", "This quote is absent."])
def test_judge_output_rejects_empty_or_nonexistent_quote(quote):
    data = case()
    data["judge"] = judge(diagnoses=(diagnosis(quote=quote),))
    result = evaluate(**data)
    assert result.judge_status == "invalid" and result.diagnosis_pass is None
    assert "JUDGE_QUOTE_NOT_FOUND" in result.failure_reasons and not result.overall


def test_judge_output_quote_matching_collapses_whitespace_on_both_sides():
    data = case()
    data["report"] = capture(
        claims=[claim("cart\n  EmptyCart\tfailed and caused checkout failures.")]
    )
    data["judge"] = judge(diagnoses=(diagnosis(quote="EmptyCart \n failed"),))
    assert evaluate(**data).overall


def test_judge_output_quote_must_belong_to_the_named_location():
    data = case()
    data["report"] = capture(summary=OBSERVATION)
    data["judge"] = judge(diagnoses=(diagnosis(location="summary", linked_claim=0),))
    result = evaluate(**data)
    assert result.judge_status == "invalid"
    assert "JUDGE_QUOTE_NOT_FOUND" in result.failure_reasons


def test_judge_output_validates_noncausal_annotations_before_diagnosis_filtering():
    data = case(fault=False)
    data["judge"] = judge(
        fault=False,
        diagnoses=(
            diagnosis(
                location="claims[9]",
                factual_causal=False,
                cited_evidence_ids=(),
            ),
        ),
    )
    result = evaluate(**data)
    assert result.judge_status == "invalid" and result.diagnosis_pass is None
    assert "JUDGE_BAD_LOCATION" in result.failure_reasons and not result.overall


@pytest.mark.parametrize(
    "location,linked_claim,cited_ids",
    [
        ("claims[0]", None, ("ev-other-claim",)),
        ("claims[0]", None, ("ev-invented",)),
        ("summary", None, (EID,)),
        ("gaps[0]", None, (EID,)),
        ("next_steps[0]", None, (EID,)),
        ("summary", -1, (EID,)),
        ("summary", 2, (EID,)),
        ("summary", 1, (EID,)),
    ],
)
def test_judge_output_rejects_unknown_borrowed_or_unlinked_citations(
    location, linked_claim, cited_ids
):
    data = case()
    data["report"] = capture(
        claims=[claim(), claim(OBSERVATION, evidence_ids=["ev-other-claim"])],
        gaps=[CAUSE],
        next_steps=[CAUSE],
    )
    data["delivered"]["ev-other-claim"] = record(evidence_id="ev-other-claim", rows=[])
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID, "ev-other-claim"))
    data["judge"] = judge(
        diagnoses=(
            diagnosis(
                location=location,
                linked_claim=linked_claim,
                cited_evidence_ids=cited_ids,
            ),
        )
    )
    result = evaluate(**data)
    assert result.gate.passed
    assert result.judge_status == "invalid" and result.diagnosis_pass is None
    assert "JUDGE_CITATION_NOT_IN_CLAIM" in result.failure_reasons
    assert result.failure_class == "judge_error" and not result.overall


@pytest.mark.parametrize(
    "fault,diagnoses,verdict",
    [
        (True, (diagnosis(),), False),
        (True, (), True),
        (False, (), False),
        (False, (diagnosis(),), True),
        (True, (diagnosis(),), None),
    ],
)
def test_judge_output_verdict_must_equal_recomputed_diagnosis_result(
    fault, diagnoses, verdict
):
    data = case(fault=fault)
    data["report"] = capture()
    data["judge"] = judge(diagnoses=diagnoses, verdict=verdict)
    result = evaluate(**data)
    assert result.judge_status == "invalid" and result.diagnosis_pass is None
    assert "JUDGE_VERDICT_MISMATCH" in result.failure_reasons
    assert result.failure_class == "judge_error" and not result.overall


@pytest.mark.parametrize("deterministic_pass", [False, True])
@pytest.mark.parametrize("judge_pass", [False, True])
def test_overall_is_deterministic_and_judge_truth_table(deterministic_pass, judge_pass):
    data = case()
    if not deterministic_pass:
        data["traces"] = (trace(read_back="root_run_not_found"),)
    data["judge"] = judge(
        diagnoses=(diagnosis(),) if judge_pass else (), verdict=judge_pass
    )
    result = evaluate(**data)
    assert result.gate.passed and result.deterministic_pass is deterministic_pass
    assert result.judge_pass is judge_pass
    assert result.overall is (deterministic_pass and judge_pass)


def test_overall_failed_gate_cannot_be_overridden_by_positive_judge():
    data = case()
    data["outcome"] = replace(data["outcome"], permissions=("write",))
    result = evaluate(**data)
    assert (
        not result.gate.passed and not result.deterministic_pass and not result.overall
    )
    assert result.failure_class == "deterministic_gate"
    assert "PERMISSION_VIOLATION" in result.failure_reasons


@pytest.mark.parametrize("fault", [False, True])
def test_judge_request_exact_keys_and_evaluator_only_input_boundary(fault):
    data = case(fault=fault)
    data["delivered"]["ev-unused"] = record(evidence_id="ev-unused")
    data["delivered"]["ev-undelivered"] = record(
        evidence_id="ev-undelivered", delivered=False
    )
    data["outcome"] = replace(
        data["outcome"], evidence_ids=(EID, "ev-unused", "ev-undelivered")
    )
    payload = report_payload(fault=fault)
    payload["claims"].append(claim(OBSERVATION, evidence_ids=["ev-undelivered"]))
    data["report"] = capture(payload)
    args = {
        key: data[key]
        for key in ("spec", "answer", "report", "delivered", "authorized", "outcome")
    }
    result = judge_request(**args)
    assert set(result) == {
        "question",
        "subject_id",
        "authorized_windows",
        "report_raw",
        "cited_evidence",
        "expected_output",
        "scenario_kind",
        "injected",
        "signature_matches",
    }
    assert result["question"] == data["spec"].stimulus.question
    assert result["subject_id"] == data["spec"].stimulus.subject_id
    assert set(result["authorized_windows"]) == {SCOPE}
    bounds = result["authorized_windows"][SCOPE]
    assert set(bounds) == {"start", "end"}
    assert datetime.fromisoformat(bounds["start"]) == START
    assert datetime.fromisoformat(bounds["end"]) == END
    assert result["report_raw"] == data["report"].raw
    assert tuple(result["expected_output"]) == data["answer"].expected_output
    assert result["scenario_kind"] == data["spec"].kind
    # AMBIGUOUS: interface:130 specifies each cited record's fields but not
    # its collection type. Accept a sequence or an ID-keyed mapping without
    # weakening the exact record fields or the delivery/citation filter.
    cited = result["cited_evidence"]
    records = list(cited.values()) if isinstance(cited, Mapping) else list(cited)
    assert len(records) == 1
    evidence = records[0]
    assert evidence == {
        "evidence_id": EID,
        "view_sha256": canonical_hash(data["delivered"][EID].view),
        "view": data["delivered"][EID].view,
    }
    if fault:
        assert set(result["injected"]) == {
            "service",
            "service_aliases",
            "operation",
            "operation_aliases",
        }
        assert result["injected"]["service"] == "cart"
        assert result["injected"]["operation"] == OPERATION
        assert tuple(result["injected"]["service_aliases"]) == ("cartservice",)
        assert tuple(result["injected"]["operation_aliases"]) == ("EmptyCart",)
        assert result["signature_matches"][EID] == ["span-cart"]
        assert set(result["signature_matches"]) == {EID}
    else:
        assert result["injected"] is None
        assert not result["signature_matches"]
    serialized = json.dumps(result)
    for excluded in (
        "ev-unused",
        "private-cart-flag",
        "private-variant",
        data["spec"].scenario_id,
    ):
        assert excluded not in serialized
    assert "ev-undelivered" not in json.dumps(result["cited_evidence"])


# Group 5: calibration counts, exact inclusive thresholds, input errors (P7).


@pytest.mark.parametrize(
    "positive_errors,passed,agreement", [(0, True, 48), (2, True, 46), (3, False, 45)]
)
def test_calibrate_agreement_boundary_46_of_48(positive_errors, passed, agreement):
    labels, verdicts = calibration_inputs()
    for i in range(24, 24 + positive_errors):
        verdicts[i] = (verdicts[i][0], False)
    result = calibrate(labels, verdicts)
    assert (result.items, result.comparisons, result.agreement) == (24, 48, agreement)
    assert result.negatives == 12 and result.false_passes == 0
    assert result.passed is passed
    assert result.reasons == (() if passed else ("AGREEMENT_BELOW_THRESHOLD",))


@pytest.mark.parametrize("negatives,passed", [(11, False), (12, True), (13, True)])
def test_calibrate_requires_at_least_twelve_negative_items(negatives, passed):
    labels, verdicts = calibration_inputs(negatives=negatives)
    result = calibrate(labels, verdicts)
    assert result.negatives == negatives and result.false_passes == 0
    assert result.passed is passed
    assert result.reasons == (() if passed else ("TOO_FEW_NEGATIVES",))


@pytest.mark.parametrize("false_passes", [1, 2])
def test_calibrate_any_negative_false_pass_fails_even_with_enough_agreement(
    false_passes,
):
    labels, verdicts = calibration_inputs()
    for i in range(false_passes):
        verdicts[i] = (verdicts[i][0], True)
    result = calibrate(labels, verdicts)
    assert result.agreement == 48 - false_passes
    assert result.false_passes == false_passes and not result.passed
    assert result.reasons == ("FALSE_PASS",)


@pytest.mark.parametrize("items,passed", [(23, False), (24, True), (25, True)])
def test_calibrate_minimum_item_boundary(items, passed):
    labels, verdicts = calibration_inputs(items=items)
    result = calibrate(labels, verdicts)
    assert result.items == items and result.comparisons == items * 2
    assert result.passed is passed
    assert result.reasons == (() if passed else ("TOO_FEW_ITEMS",))


def test_calibrate_retains_all_threshold_reasons_in_contract_order():
    labels, verdicts = calibration_inputs(items=2, negatives=1)
    verdicts[0] = (verdicts[0][0], True)
    result = calibrate(labels, verdicts)
    assert not result.passed
    assert (
        result.items,
        result.comparisons,
        result.agreement,
        result.negatives,
        result.false_passes,
    ) == (2, 4, 3, 1, 1)
    assert result.reasons == (
        "TOO_FEW_ITEMS",
        "AGREEMENT_BELOW_THRESHOLD",
        "TOO_FEW_NEGATIVES",
        "FALSE_PASS",
    )


def test_calibrate_empty_dataset_is_incomplete_and_custom_thresholds_are_respected():
    empty = calibrate({}, [])
    assert (
        empty.items,
        empty.comparisons,
        empty.agreement,
        empty.negatives,
        empty.false_passes,
    ) == (0, 0, 0, 0, 0)
    assert not empty.passed
    assert empty.reasons == (
        "TOO_FEW_ITEMS",
        "AGREEMENT_BELOW_THRESHOLD",
        "TOO_FEW_NEGATIVES",
    )
    labels, verdicts = calibration_inputs(items=4, negatives=2, runs=3)
    result = calibrate(
        labels,
        verdicts,
        min_items=4,
        runs_per_item=3,
        min_agreement=12,
        min_negatives=2,
    )
    assert result.passed and result.comparisons == 12 and result.agreement == 12


@pytest.mark.parametrize(
    "mutation", ["unlabeled", "missing_once", "missing_item", "extra_run"]
)
def test_calibrate_rejects_missing_labels_or_inexact_per_item_run_count(mutation):
    labels, verdicts = calibration_inputs()
    if mutation == "unlabeled":
        verdicts.append(("unlabeled-item", True))
    elif mutation == "missing_once":
        verdicts.pop()
    elif mutation == "missing_item":
        verdicts = verdicts[:-2]
    else:
        verdicts.append(verdicts[0])
    with pytest.raises(CalibrationError):
        calibrate(labels, verdicts)


def test_calibrate_label_and_verdict_order_does_not_change_counts():
    labels, verdicts = calibration_inputs()
    assert calibrate(labels, verdicts) == calibrate(
        dict(reversed(list(labels.items()))), list(reversed(verdicts))
    )


# Group 6: required trace identities and complete frozen manifests (P2/P4/P13).


@pytest.mark.parametrize(
    "read_back", ["root_run_not_found", "no_trace_id", "not_exported", "error"]
)
def test_trace_required_readback_failure_preserves_judge_but_fails_run(read_back):
    data = case()
    data["traces"] = (trace(read_back=read_back),)
    result = evaluate(**data)
    assert result.evidence_status == "incomplete" and not result.deterministic_pass
    assert (
        result.judge_status == "ok"
        and result.diagnosis_pass is True
        and result.judge_pass
    )
    assert not result.overall and result.failure_class == "evidence_incomplete"
    assert result.failure_reasons == ("EVIDENCE_INCOMPLETE",)


@pytest.mark.parametrize(
    "run_id,expected",
    [
        ("other-product", "product-1"),
        (None, "product-1"),
        ("product-1", None),
        (None, None),
    ],
)
def test_trace_found_still_requires_matching_nonnull_identity(run_id, expected):
    data = case()
    data["traces"] = (trace(run_id=run_id, expected_run_id=expected),)
    result = evaluate(**data)
    assert result.evidence_status == "incomplete" and result.judge_pass
    assert not result.deterministic_pass and not result.overall
    assert "EVIDENCE_INCOMPLETE" in result.failure_reasons


@pytest.mark.parametrize("traces", [(), (trace(required=False),)])
def test_trace_requires_at_least_one_required_entry(traces):
    data = case()
    data["traces"] = traces
    result = evaluate(**data)
    assert result.evidence_status == "incomplete" and not result.overall


def test_trace_optional_missing_entry_does_not_invalidate_required_roots():
    data = case()
    data["traces"] = (
        trace(role="attempt", run_id="attempt-1", expected_run_id="attempt-1"),
        trace(),
        trace(role="evaluation", required=False, read_back="no_trace_id", run_id=None),
    )
    result = evaluate(**data)
    assert result.evidence_status == "complete" and result.overall


@pytest.mark.parametrize("reverse", [False, True])
def test_trace_multiple_roots_are_checked_individually_not_first_entry(reverse):
    data = case()
    traces = [
        trace(role="attempt", run_id="attempt-1", expected_run_id="attempt-1"),
        trace(role="product_run", run_id="attempt-1", expected_run_id="product-1"),
        trace(role="evaluation", run_id="evaluation-1", expected_run_id="evaluation-1"),
    ]
    data["traces"] = tuple(reversed(traces)) if reverse else tuple(traces)
    result = evaluate(**data)
    assert result.evidence_status == "incomplete" and not result.overall


def test_trace_setup_failure_keeps_attempt_root_without_fabricating_product_run():
    data = case()
    data["attempt"] = replace(
        data["attempt"], status="setup_failed", product_run_id=None
    )
    data["outcome"] = None
    data["report"] = None
    data["judge"] = None
    data["traces"] = (
        trace(
            role="attempt",
            run_id="attempt-1",
            expected_run_id="attempt-1",
            product_run_id=None,
        ),
    )
    result = evaluate(**data)
    assert result.evidence_status == "complete"
    assert result.attempt.product_run_id is None and result.failure_class == "setup"
    assert "SETUP_FAILED" in result.failure_reasons and not result.overall


def test_evaluate_preserves_input_records_report_hash_and_judge_verdict():
    data = case()
    original = deepcopy(data)
    old_hash = data["delivered"][EID].view_sha256
    data["traces"] = (trace(read_back="root_run_not_found"),)
    evaluate(**data)
    for key in ("report", "delivered", "judge", "answer", "outcome", "authorized"):
        assert data[key] == original[key]
    assert data["delivered"][EID].view_sha256 == old_hash
    # AMBIGUOUS: contract:153/198 require legacy summary adaptation/hash
    # preservation, but interface:16-150 exports no summary reader/revision API.
    # This only verifies read-only behavior of the exposed DTO seam; legacy
    # serialized-summary compatibility cannot be asserted through that seam.


def test_manifest_required_dimensions_are_exact_and_hash_matches_canonical_json():
    frozen = manifest()
    assert REQUIRED_DIMENSIONS == DIMENSIONS
    validate_manifest(frozen)
    assert manifest_unknowns(frozen) == ()
    assert manifest_hash(frozen) == canonical_hash(asdict(frozen))
    reversed_manifest = replace(
        frozen, dimensions=dict(reversed(list(frozen.dimensions.items())))
    )
    assert manifest_hash(reversed_manifest) == manifest_hash(frozen)
    assert manifest_hash(replace(frozen, round_id="different-round")) != manifest_hash(
        frozen
    )
    changed = {**frozen.dimensions, "prompt": DimensionValue("version", "v2", None)}
    assert manifest_hash(replace(frozen, dimensions=changed)) != manifest_hash(frozen)


def test_manifest_valid_hash_and_none_are_included_in_canonical_identity():
    frozen = manifest()
    dimensions = {
        **frozen.dimensions,
        "code": DimensionValue("hash", canonical_hash({"commit": "contract"}), None),
        "human_feedback": DimensionValue("none", None, "no feedback in this round"),
    }
    frozen = replace(frozen, dimensions=dimensions)
    validate_manifest(frozen)
    assert manifest_unknowns(frozen) == ()
    assert manifest_hash(frozen) == canonical_hash(asdict(frozen))
    revised = replace(
        frozen,
        dimensions={
            **dimensions,
            "human_feedback": DimensionValue(
                "none", None, "no human feedback by design"
            ),
        },
    )
    assert manifest_hash(revised) != manifest_hash(frozen)


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("unknown", [False, True])
def test_manifest_each_missing_or_unknown_dimension_is_reported_without_validation_error(
    dimension, unknown
):
    frozen = manifest()
    dimensions = dict(frozen.dimensions)
    if unknown:
        dimensions[dimension] = DimensionValue("unknown", None, "not obtained yet")
    else:
        del dimensions[dimension]
    frozen = replace(frozen, dimensions=dimensions)
    validate_manifest(frozen)
    assert manifest_unknowns(frozen) == (dimension,)


def test_manifest_unknowns_follow_required_order_not_input_order():
    frozen = manifest(
        dimensions={
            "clock_policy": DimensionValue("unknown", None, "missing"),
            "code": DimensionValue("unknown", None, "missing"),
        }
    )
    validate_manifest(frozen)
    assert manifest_unknowns(frozen) == DIMENSIONS


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_manifest_explicit_none_with_reason_is_known(dimension):
    frozen = manifest()
    dimensions = {
        **frozen.dimensions,
        dimension: DimensionValue("none", None, "not applicable to this offline round"),
    }
    frozen = replace(frozen, dimensions=dimensions)
    validate_manifest(frozen)
    assert manifest_unknowns(frozen) == ()


@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("kind", ["hash", "version"])
def test_manifest_rejects_empty_hash_or_version_value(kind, value):
    frozen = manifest()
    frozen = replace(
        frozen,
        dimensions={**frozen.dimensions, "code": DimensionValue(kind, value, None)},
    )
    with pytest.raises(ManifestError):
        validate_manifest(frozen)


@pytest.mark.parametrize("reason", [None, ""])
def test_manifest_none_requires_explicit_nonempty_reason(reason):
    frozen = manifest()
    frozen = replace(
        frozen,
        dimensions={
            **frozen.dimensions,
            "replay_fixture": DimensionValue("none", None, reason),
        },
    )
    with pytest.raises(ManifestError):
        validate_manifest(frozen)


def test_manifest_rejects_dimension_outside_fixed_list():
    frozen = manifest()
    frozen = replace(
        frozen,
        dimensions={
            **frozen.dimensions,
            "unfrozen_extra": DimensionValue("version", "v1", None),
        },
    )
    with pytest.raises(ManifestError):
        validate_manifest(frozen)


# Group 7: attempt start, ordered failure classes, aggregation and baseline.


def test_evaluate_refuses_unstarted_attempt():
    data = case()
    data["attempt"] = replace(data["attempt"], started=False)
    with pytest.raises(ValueError):
        evaluate(**data)


@pytest.mark.parametrize(
    "status,reason",
    [
        ("setup_failed", "SETUP_FAILED"),
        ("aborted", "ATTEMPT_ABORTED"),
        ("unknown", "ATTEMPT_UNKNOWN"),
    ],
)
def test_evaluate_started_setup_abort_and_unknown_are_failures(status, reason):
    data = case()
    data["attempt"] = replace(data["attempt"], status=status)
    result = evaluate(**data)
    assert result.failure_class == "setup" and result.failure_reasons == (reason,)
    assert not result.deterministic_pass and not result.overall


def test_evaluate_replay_miss_is_failure_even_when_report_and_judge_pass():
    data = case()
    data["attempt"] = replace(data["attempt"], replay_miss=True)
    result = evaluate(**data)
    assert result.gate.passed and result.judge_pass
    assert not result.deterministic_pass and not result.overall
    assert result.failure_class == "replay_miss" and result.failure_reasons == (
        "REPLAY_MISS",
    )


@pytest.mark.parametrize(
    "primary,expected_reasons",
    [
        (
            "setup",
            {
                "SETUP_FAILED",
                "REPLAY_MISS",
                "EXECUTION_NOT_COMPLETED",
                "PERMISSION_VIOLATION",
                "JUDGE_TIMEOUT",
                "EVIDENCE_INCOMPLETE",
            },
        ),
        (
            "replay_miss",
            {
                "REPLAY_MISS",
                "EXECUTION_NOT_COMPLETED",
                "PERMISSION_VIOLATION",
                "JUDGE_TIMEOUT",
                "EVIDENCE_INCOMPLETE",
            },
        ),
        (
            "execution",
            {
                "EXECUTION_NOT_COMPLETED",
                "PERMISSION_VIOLATION",
                "JUDGE_TIMEOUT",
                "EVIDENCE_INCOMPLETE",
            },
        ),
        (
            "deterministic_gate",
            {"PERMISSION_VIOLATION", "JUDGE_TIMEOUT", "EVIDENCE_INCOMPLETE"},
        ),
        ("judge_error", {"JUDGE_TIMEOUT", "EVIDENCE_INCOMPLETE"}),
        ("diagnosis", {"DIAGNOSIS_FAILED", "EVIDENCE_INCOMPLETE"}),
        ("evidence_incomplete", {"EVIDENCE_INCOMPLETE"}),
    ],
)
def test_evaluate_failure_priority_preserves_lower_priority_reasons(
    primary, expected_reasons
):
    data = case()
    if "SETUP_FAILED" in expected_reasons:
        data["attempt"] = replace(data["attempt"], status="setup_failed")
    if "REPLAY_MISS" in expected_reasons:
        data["attempt"] = replace(data["attempt"], replay_miss=True)
    if "EXECUTION_NOT_COMPLETED" in expected_reasons:
        data["outcome"] = replace(data["outcome"], final_state="failed")
    if "PERMISSION_VIOLATION" in expected_reasons:
        data["outcome"] = replace(data["outcome"], permissions=("write",))
    if "JUDGE_TIMEOUT" in expected_reasons:
        data["judge"] = judge(status="timeout", verdict=None, diagnoses=())
    elif "DIAGNOSIS_FAILED" in expected_reasons:
        data["judge"] = judge(diagnoses=(), verdict=False)
    data["traces"] = (trace(read_back="root_run_not_found"),)
    result = evaluate(**data)
    assert result.failure_class == primary
    assert set(result.failure_reasons) == expected_reasons
    assert not result.overall


def test_gate_and_evaluate_retain_all_independent_binding_failure_reasons():
    data = case()
    data["outcome"] = replace(
        data["outcome"], final_state="failed", permissions=("write",)
    )
    data["delivered"][EID] = record(
        view_sha256="bad-hash",
        status="error",
        citable_as_fact=False,
        target_id="other-target",
        time_scope_refs=frozenset({"other-window"}),
        window=Window(START - timedelta(seconds=1), END),
        rows=[span(status_tags={})],
    )
    expected = {
        "EXECUTION_NOT_COMPLETED",
        "PERMISSION_VIOLATION",
        "VIEW_HASH_MISMATCH",
        "CITATION_NOT_FACT_EVIDENCE",
        "TARGET_MISMATCH",
        "TIME_SCOPE_MISMATCH",
        "WINDOW_OUTSIDE_AUTHORIZED",
        "SIGNATURE_ABSENT",
    }
    result = run_gate(**gate_args(data))
    assert not result.passed and set(result.reasons) == expected
    result = evaluate(**data)
    assert expected <= set(result.failure_reasons)
    assert result.failure_class == "execution" and not result.overall


def planned_for(attempt_record):
    return PlannedSlot(
        attempt_record.scenario_id,
        attempt_record.scenario_version,
        attempt_record.mode,
        attempt_record.repetition,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_planned",
        "wrong_round",
        "wrong_manifest",
        "duplicate_attempt_id",
        "unplanned_scenario",
        "unplanned_version",
        "unplanned_mode",
        "unplanned_repetition",
        "duplicate_slot",
    ],
)
def test_aggregate_rejects_duplicate_identity_and_incompatible_or_unplanned_evaluations(
    mutation,
):
    first = attempt()
    planned = [planned_for(first)]
    results = [evaluation(attempt_record=first)]
    if mutation == "duplicate_planned":
        planned.append(planned[0])
    elif mutation in {"duplicate_attempt_id", "duplicate_slot"}:
        second = (
            replace(first, repetition=2)
            if mutation == "duplicate_attempt_id"
            else replace(first, attempt_id="attempt-2")
        )
        if mutation == "duplicate_attempt_id":
            planned.append(planned_for(second))
        results.append(evaluation(attempt_record=second))
    else:
        changes = {
            "wrong_round": {"round_id": "other-round"},
            "wrong_manifest": {"manifest_hash": "wrong-hash"},
            "unplanned_scenario": {"scenario_id": "other-scenario"},
            "unplanned_version": {"scenario_version": "v2"},
            "unplanned_mode": {"mode": "live"},
            "unplanned_repetition": {"repetition": 2},
        }[mutation]
        results = [evaluation(attempt_record=replace(first, **changes))]
    with pytest.raises(AggregateError):
        aggregate(manifest(), planned, results, calibration())


def test_aggregate_started_failures_all_enter_denominator_unstarted_is_separate():
    results = []
    for i, failure in enumerate(
        (
            None,
            "setup_failed",
            "aborted",
            "unknown",
            "replay_miss",
            "execution",
            "gate",
            "judge",
            "diagnosis",
            "trace",
        ),
        1,
    ):
        data = case()
        data["attempt"] = replace(
            data["attempt"], attempt_id=f"attempt-{i}", repetition=i
        )
        if failure in {"setup_failed", "aborted", "unknown"}:
            data["attempt"] = replace(
                data["attempt"], status=failure, product_run_id=None
            )
            data["outcome"] = None
            data["report"] = None
            data["judge"] = None
            data["traces"] = (
                trace(
                    role="attempt",
                    run_id=f"attempt-{i}",
                    expected_run_id=f"attempt-{i}",
                    product_run_id=None,
                ),
            )
        elif failure == "replay_miss":
            data["attempt"] = replace(data["attempt"], replay_miss=True)
        elif failure == "execution":
            data["outcome"] = replace(data["outcome"], final_state="failed")
        elif failure == "gate":
            data["outcome"] = replace(data["outcome"], permissions=("write",))
        elif failure == "judge":
            data["judge"] = judge(status="timeout", verdict=None, diagnoses=())
        elif failure == "diagnosis":
            data["judge"] = judge(diagnoses=(), verdict=False)
        elif failure == "trace":
            data["traces"] = ()
        results.append(evaluate(**data))
    planned = [planned_for(result.attempt) for result in results]
    planned.append(PlannedSlot("contract-fault", "v1", "replay", 11))
    result = aggregate(manifest(), planned, results, calibration())
    assert result.round_id == manifest().round_id
    assert result.manifest_hash == canonical_hash(asdict(manifest()))
    assert len(result.rows) == 1
    row = result.rows[0]
    assert (row.planned, row.attempted, row.not_started, row.passed, row.failed) == (
        11,
        10,
        1,
        1,
        9,
    )
    assert row.pass_rate == pytest.approx(1 / 10)
    assert row.replay_miss == 1 and row.replay_miss_rate == pytest.approx(1 / 10)
    assert row.failure_classes == {
        "setup": 3,
        "replay_miss": 1,
        "execution": 1,
        "deterministic_gate": 1,
        "judge_error": 1,
        "diagnosis": 1,
        "evidence_incomplete": 1,
    }
    assert not result.baseline_complete
    assert result.incomplete_reasons == ("SLOTS_NOT_STARTED", "EVIDENCE_INCOMPLETE")


@pytest.mark.parametrize("mode", ["replay", "live", "structural"])
def test_aggregate_zero_denominator_rates_are_none(mode):
    planned = [PlannedSlot("contract-fault", "v1", mode, 1)]
    result = aggregate(manifest(), planned, [], calibration())
    row = result.rows[0]
    assert (
        row.planned,
        row.attempted,
        row.not_started,
        row.passed,
        row.failed,
        row.replay_miss,
    ) == (1, 0, 1, 0, 0, 0)
    assert row.pass_rate is None and row.replay_miss_rate is None
    assert row.failure_classes == {} and not result.baseline_complete


def test_aggregate_separates_modes_scenarios_versions_and_sorts_rows():
    attempts = [
        attempt(attempt_id="c", scenario_id="z", mode="structural"),
        attempt(attempt_id="a", scenario_id="a", mode="replay"),
        attempt(attempt_id="b", scenario_id="a", mode="live"),
        attempt(attempt_id="d", scenario_id="a", scenario_version="v2", mode="replay"),
    ]
    planned = [planned_for(item) for item in attempts]
    results = [evaluation(attempt_record=item) for item in attempts]
    result = aggregate(manifest(), planned, results, calibration())
    assert [(r.scenario_id, r.scenario_version, r.mode) for r in result.rows] == [
        ("a", "v1", "live"),
        ("a", "v1", "replay"),
        ("a", "v2", "replay"),
        ("z", "v1", "structural"),
    ]
    for row in result.rows:
        assert (
            row.planned,
            row.attempted,
            row.not_started,
            row.passed,
            row.failed,
        ) == (1, 1, 0, 1, 0)
        assert row.pass_rate == (None if row.mode == "structural" else 1.0)
        assert row.replay_miss_rate == (0.0 if row.mode == "replay" else None)
    # AMBIGUOUS: contract:159/199 says incompatible versions must be refused;
    # interface:148-149 defines planned matching and grouping by version, but
    # no compatibility relation. Only explicitly planned versions coexist here;
    # an unplanned version is rejected by the preceding rejection test.


def test_aggregate_structural_never_reports_quality_rate_even_after_pass():
    item = attempt(mode="structural")
    result = aggregate(
        manifest(),
        [planned_for(item)],
        [evaluation(attempt_record=item)],
        calibration(),
    )
    row = result.rows[0]
    assert row.attempted == 1 and row.passed == 1
    assert row.pass_rate is None and row.replay_miss_rate is None
    assert not result.baseline_complete and result.incomplete_reasons == (
        "NO_QUALITY_SLOTS",
    )
    # AMBIGUOUS: contract:199 also asks for zero models/credentials in structural
    # replay; interface:5/152 supplies no replay driver. This pure aggregate DTO
    # test cannot establish the future driver's no-network/no-credential behavior.


def test_aggregate_unknown_started_attempt_is_failed_not_not_started():
    data = case()
    data["attempt"] = replace(data["attempt"], status="unknown", product_run_id=None)
    data["outcome"] = None
    data["report"] = None
    data["judge"] = None
    data["traces"] = (
        trace(
            role="attempt",
            run_id="attempt-1",
            expected_run_id="attempt-1",
            product_run_id=None,
        ),
    )
    result = aggregate(
        manifest(), [planned_for(data["attempt"])], [evaluate(**data)], calibration()
    )
    row = result.rows[0]
    assert (row.attempted, row.failed, row.not_started) == (1, 1, 0)
    assert row.failure_classes == {"setup": 1} and row.pass_rate == 0.0
    # AMBIGUOUS: contract:159 counts started slots lacking summary as unknown;
    # interface:144-149 receives no started-attempt ledger and treats missing
    # evaluations as not_started. Represent an explicitly known start with an
    # unknown evaluation; cannot infer a missing start from planned alone.


@pytest.mark.parametrize("failure", [None, "diagnosis"])
def test_aggregate_baseline_can_complete_below_one_hundred_percent_success(failure):
    item = attempt()
    result = aggregate(
        manifest(),
        [planned_for(item)],
        [evaluation(attempt_record=item, failure=failure)],
        calibration(),
    )
    assert result.baseline_complete and result.incomplete_reasons == ()
    assert result.rows[0].pass_rate == (1.0 if failure is None else 0.0)


@pytest.mark.parametrize(
    "missing",
    [
        "slot",
        "evidence",
        "manifest",
        "calibration_none",
        "calibration_failed",
        "quality",
    ],
)
def test_aggregate_each_baseline_completion_requirement(missing):
    frozen = manifest()
    item = attempt(mode="structural" if missing == "quality" else "replay")
    if missing == "manifest":
        frozen = replace(
            frozen,
            dimensions={
                **frozen.dimensions,
                "prompt": DimensionValue("unknown", None, "not frozen"),
            },
        )
        item = replace(item, manifest_hash=canonical_hash(asdict(frozen)))
    results = [evaluation(attempt_record=item)]
    if missing == "slot":
        results = []
    elif missing == "evidence":
        results = [
            evaluation(
                attempt_record=item,
                failure="evidence_incomplete",
                evidence="incomplete",
            )
        ]
    calibration_result = calibration()
    if missing == "calibration_none":
        calibration_result = None
    elif missing == "calibration_failed":
        calibration_result = calibration(
            passed=False, reasons=("FALSE_PASS",), false_passes=1
        )
    result = aggregate(frozen, [planned_for(item)], results, calibration_result)
    reason = {
        "slot": "SLOTS_NOT_STARTED",
        "evidence": "EVIDENCE_INCOMPLETE",
        "manifest": "MANIFEST_UNKNOWN",
        "calibration_none": "JUDGE_NOT_CALIBRATED",
        "calibration_failed": "JUDGE_NOT_CALIBRATED",
        "quality": "NO_QUALITY_SLOTS",
    }[missing]
    assert not result.baseline_complete and result.incomplete_reasons == (reason,)


def test_aggregate_all_baseline_incomplete_reasons_preserved_in_order():
    frozen = manifest(dimensions={})
    item = attempt(mode="structural", manifest_hash=canonical_hash(asdict(frozen)))
    planned = [
        planned_for(item),
        PlannedSlot(item.scenario_id, item.scenario_version, "structural", 2),
    ]
    failed = evaluation(
        attempt_record=item, failure="evidence_incomplete", evidence="incomplete"
    )
    result = aggregate(frozen, planned, [failed], None)
    assert not result.baseline_complete
    assert result.incomplete_reasons == (
        "SLOTS_NOT_STARTED",
        "EVIDENCE_INCOMPLETE",
        "MANIFEST_UNKNOWN",
        "JUDGE_NOT_CALIBRATED",
        "NO_QUALITY_SLOTS",
    )


def test_aggregate_empty_plan_cannot_be_a_complete_baseline():
    result = aggregate(manifest(), [], [], calibration())
    assert result.rows == () and not result.baseline_complete
    assert result.incomplete_reasons == ("NO_QUALITY_SLOTS",)


def test_aggregate_and_rows_are_frozen_public_dtos():
    item = attempt()
    result = aggregate(
        manifest(),
        [planned_for(item)],
        [evaluation(attempt_record=item)],
        calibration(),
    )
    for obj in (result, result.rows[0]):
        name = fields(obj)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(obj, name, getattr(obj, name))


# Independent-review revisions: interface:118,122,127,150,171.
# Existing assertions above stay unchanged; these checks exercise the new
# semantics through public DTOs and functions only.


@pytest.mark.parametrize("kind", ["hypothesis", "recommendation"])
@pytest.mark.parametrize(
    "location", ["claims[0]", "summary", "gaps[0]", "next_steps[0]"]
)
@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "target",
        "window_before",
        "window_after",
        "not_citable",
        "not_ok",
        "missing_scope",
        "unauthorized_scope",
        "empty_targets",
    ],
)
def test_diagnosis_factual_evidence_qualification_overrides_report_kind(
    kind, location, defect
):
    data = case()
    changes = {
        "none": {},
        "target": {},
        "window_before": {"window": Window(START - timedelta(microseconds=1), END)},
        "window_after": {"window": Window(START, END + timedelta(microseconds=1))},
        "not_citable": {"citable_as_fact": False},
        "not_ok": {"status": "error"},
        "missing_scope": {"time_scope_refs": frozenset()},
        "unauthorized_scope": {},
        "empty_targets": {},
    }[defect]
    data["delivered"][EID] = record(**changes)
    cited_claim = claim(kind=kind)
    if defect == "target":
        # The record targets the scenario, but this claim does not bind to it.
        cited_claim["target_refs"] = ["other-target"]
    elif defect == "empty_targets":
        cited_claim["target_refs"] = []
    elif defect == "unauthorized_scope":
        data["authorized"] = {"other-scope": Window(START, END)}
    data["report"] = capture(claims=[cited_claim], gaps=[CAUSE], next_steps=[CAUSE])
    qualified = defect == "none"
    data["judge"] = judge(
        diagnoses=(
            diagnosis(
                location=location,
                linked_claim=None if location == "claims[0]" else 0,
            ),
        ),
        # Interface:112,125-127 (PR #221 third review): the verdict must
        # reflect the absence of an eligible signature for defective evidence.
        verdict=qualified,
    )
    result = evaluate(**data)
    # CONTRACT CHANGE, interface:111-112,121,123,127,173: raw span matching
    # still succeeds, but every defect excludes the sole claim/record from
    # eligible signatures. Thus gate and deterministic_pass now fail, and the
    # primary failure is deterministic_gate rather than diagnosis. Nonfact
    # claim kinds do not bypass the signature's fact-evidence qualification.
    assert signature_matches(rule(), data["delivered"][EID]) == ("span-cart",)
    assert result.gate.checks["citations"]
    assert result.gate.checks["signature"] is qualified
    assert result.gate.passed is qualified and result.deterministic_pass is qualified
    assert result.judge_status == "ok"
    assert "JUDGE_VERDICT_MISMATCH" not in result.failure_reasons
    assert result.diagnosis_pass is qualified
    assert result.judge_pass is qualified and result.overall is qualified
    if qualified:
        assert result.signature_matches[EID] == ("span-cart",)
        assert result.failure_class is None and result.failure_reasons == ()
    else:
        assert EID not in result.signature_matches
        assert "SIGNATURE_ABSENT" in result.failure_reasons
        assert result.failure_class == "deterministic_gate"
        assert "DIAGNOSIS_EVIDENCE_UNQUALIFIED" in result.failure_reasons


@pytest.mark.parametrize("unqualified_first", [False, True])
def test_diagnosis_requires_every_cited_record_to_qualify(unqualified_first):
    data = case()
    other_id = "ev-unqualified"
    data["delivered"][other_id] = record(evidence_id=other_id, citable_as_fact=False)
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID, other_id))
    ids = (other_id, EID) if unqualified_first else (EID, other_id)
    data["report"] = capture(claims=[claim(kind="hypothesis", evidence_ids=list(ids))])
    data["judge"] = judge(diagnoses=(diagnosis(cited_evidence_ids=ids),))
    result = evaluate(**data)
    assert result.gate.passed and result.judge_status == "ok"
    assert (
        result.diagnosis_pass is False and not result.judge_pass and not result.overall
    )
    assert result.failure_class == "diagnosis"
    assert "DIAGNOSIS_EVIDENCE_UNQUALIFIED" in result.failure_reasons


@pytest.mark.parametrize(
    "scenario_id", ["eval-payment-failure-1", "eval-payment-failure-2"]
)
@pytest.mark.parametrize(
    "row_changes,expected",
    [
        ({}, True),
        ({"operation": "Charge"}, False),
        ({"service": "cart"}, False),
        ({"operation": "grpc.oteldemo.PaymentService/charge"}, False),
        ({"status_tags": {"rpc.grpc.status_code": 0}}, False),
        ({"operation_truncated": True}, False),
        ({"start_us": micros(END + timedelta(microseconds=1))}, False),
    ],
)
def test_payment_scenario_signature_accepts_only_qualified_server_span(
    scenario_id, row_changes, expected
):
    spec, answer = load_scenario(SCENARIOS / scenario_id)
    assert answer.signature is not None
    row = span(
        service="payment",
        operation="grpc.oteldemo.PaymentService/Charge",
        status_tags={"rpc.grpc.status_code": 13},
        error_by_visible_tags=False,
        span_id="payment-server-error",
    )
    row.update(row_changes)
    visible = record(rows=[row], target_id=spec.stimulus.target_id)
    assert signature_matches(answer.signature, visible) == (
        ("payment-server-error",) if expected else ()
    )
    data = case()
    data["spec"], data["answer"] = spec, answer
    data["outcome"] = replace(data["outcome"], scenario_id=spec.scenario_id)
    data["report"] = capture(
        claims=[
            claim(
                "The payment Charge RPC returned an error.",
                target_refs=[spec.stimulus.target_id],
            )
        ]
    )
    data["delivered"] = {EID: visible}
    result = run_gate(**gate_args(data))
    assert result.checks["citations"]
    assert result.checks["signature"] is expected and result.passed is expected
    assert result.reasons == (() if expected else ("SIGNATURE_ABSENT",))


@pytest.mark.parametrize(
    "owner,field",
    [
        ("attempt", "scenario_id"),
        ("attempt", "scenario_version"),
        ("answer", "scenario_id"),
        ("answer", "version"),
    ],
)
def test_evaluate_rejects_attempt_or_answer_scenario_mismatch(owner, field):
    data = case()
    data[owner] = replace(data[owner], **{field: "different"})
    with pytest.raises(ValueError, match="^SCENARIO_MISMATCH$"):
        evaluate(**data)


@pytest.mark.parametrize("fault", [False, True])
def test_evaluate_accepts_consistent_changed_scenario_identity_and_version(fault):
    data = case(fault=fault)
    ident, version = "matching-contract-scenario", "v2"
    data["spec"] = replace(
        data["spec"],
        scenario_id=ident,
        version=version,
        incident=replace(data["spec"].incident, scenario_id=ident),
    )
    data["answer"] = replace(data["answer"], scenario_id=ident, version=version)
    data["attempt"] = replace(
        data["attempt"], scenario_id=ident, scenario_version=version
    )
    data["outcome"] = replace(data["outcome"], scenario_id=ident)
    result = evaluate(**data)
    assert (
        result.overall and result.failure_class is None and result.failure_reasons == ()
    )


@pytest.mark.parametrize("role", ["attempt", "product_run", "evaluation"])
@pytest.mark.parametrize(
    "attempt_product,trace_product,complete",
    [
        ("product-1", "product-1", True),
        ("product-2", "product-2", True),
        ("product-1", "product-2", False),
        ("product-1", None, False),
        (None, None, False),
        (None, "product-1", False),
    ],
)
def test_trace_required_product_binding_and_ran_product_presence(
    role, attempt_product, trace_product, complete
):
    data = case()
    data["attempt"] = replace(data["attempt"], product_run_id=attempt_product)
    data["traces"] = (
        trace(
            role=role,
            run_id="trace-root",
            expected_run_id="trace-root",
            product_run_id=trace_product,
        ),
    )
    result = evaluate(**data)
    assert result.gate.passed and result.judge_pass
    assert result.evidence_status == ("complete" if complete else "incomplete")
    assert result.deterministic_pass is complete and result.overall is complete
    assert result.failure_class == (None if complete else "evidence_incomplete")
    assert result.failure_reasons == (() if complete else ("EVIDENCE_INCOMPLETE",))


@pytest.mark.parametrize("reverse", [False, True])
def test_trace_every_required_root_must_bind_to_attempt_product(reverse):
    data = case()
    roots = (
        trace(),
        trace(
            role="evaluation",
            run_id="eval-root",
            expected_run_id="eval-root",
            product_run_id="another-product",
        ),
    )
    data["traces"] = tuple(reversed(roots)) if reverse else roots
    result = evaluate(**data)
    assert result.evidence_status == "incomplete" and not result.overall
    assert result.judge_pass and result.failure_reasons == ("EVIDENCE_INCOMPLETE",)


@pytest.mark.parametrize(
    "trace_product,complete", [(None, True), ("stale-product", False)]
)
def test_trace_setup_root_without_product_requires_null_binding(
    trace_product, complete
):
    data = case()
    data["attempt"] = replace(
        data["attempt"], status="setup_failed", product_run_id=None
    )
    data["outcome"] = data["report"] = data["judge"] = None
    data["traces"] = (
        trace(
            role="attempt",
            run_id="attempt-root",
            expected_run_id="attempt-root",
            product_run_id=trace_product,
        ),
    )
    result = evaluate(**data)
    assert result.evidence_status == ("complete" if complete else "incomplete")
    assert not result.overall and result.failure_class == "setup"
    assert "SETUP_FAILED" in result.failure_reasons
    assert ("EVIDENCE_INCOMPLETE" in result.failure_reasons) is (not complete)


@pytest.mark.parametrize("reason", [None, "", "not applicable to this round"])
def test_aggregate_validates_manifest_none_reason(reason):
    frozen = manifest()
    frozen = replace(
        frozen,
        dimensions={
            **frozen.dimensions,
            "human_feedback": DimensionValue("none", None, reason),
        },
    )
    item = attempt(manifest_hash=canonical_hash(asdict(frozen)))
    planned = [planned_for(item)]
    results = [evaluation(attempt_record=item)]
    if not reason:
        with pytest.raises(ManifestError):
            aggregate(frozen, planned, results, calibration())
    else:
        result = aggregate(frozen, planned, results, calibration())
        assert result.baseline_complete and result.incomplete_reasons == ()
        assert result.rows[0].pass_rate == 1.0


# Outcome scenario binding: interface:118; None remains legal for setup failure.


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
def test_evaluate_rejects_outcome_scenario_mismatch(fault):
    data = case(fault=fault)
    data["outcome"] = replace(data["outcome"], scenario_id="other-scenario")
    with pytest.raises(ValueError, match="^SCENARIO_MISMATCH$"):
        evaluate(**data)


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
def test_evaluate_accepts_matching_outcome_scenario(fault):
    data = case(fault=fault)
    assert data["outcome"].scenario_id == data["spec"].scenario_id
    result = evaluate(**data)
    assert result.overall and result.failure_class is None
    assert result.failure_reasons == ()


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
def test_evaluate_accepts_none_outcome_for_setup_failure(fault):
    data = case(fault=fault)
    data["attempt"] = replace(
        data["attempt"], status="setup_failed", product_run_id=None
    )
    data["outcome"] = data["report"] = data["judge"] = None
    data["traces"] = (
        trace(
            role="attempt",
            run_id="attempt-root",
            expected_run_id="attempt-root",
            product_run_id=None,
        ),
    )
    result = evaluate(**data)
    assert result.failure_class == "setup" and not result.overall
    assert "SETUP_FAILED" in result.failure_reasons
    assert "SCENARIO_MISMATCH" not in result.failure_reasons
    assert result.evidence_status == "complete"


# PR #221: interface:102,111-112,127,130-132,173.


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
@pytest.mark.parametrize(
    "kind",
    [
        "fact",
        "counter_evidence",
        "rejected_hypothesis",
        "hypothesis",
        "recommendation",
    ],
)
@pytest.mark.parametrize("in_run", [False, True])
def test_gate_citations_and_signature_require_current_run_membership(
    fault, kind, in_run
):
    data = case(fault=fault)
    data["report"] = capture(
        fault=fault,
        claims=[
            claim(
                CAUSE if fault else OBSERVATION,
                kind=kind,
            )
        ],
    )
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID,) if in_run else ())
    gate = run_gate(**gate_args(data))
    assert gate.checks["citations"] is in_run and gate.passed is in_run
    assert gate.checks["signature"] is (in_run or not fault)
    assert ("CITATION_NOT_IN_RUN" in gate.reasons) is (not in_run)
    assert ("SIGNATURE_ABSENT" in gate.reasons) is (fault and not in_run)
    if not in_run:
        data["judge"] = None
    result = evaluate(**data)
    assert result.overall is in_run and result.deterministic_pass is in_run
    if fault and in_run:
        assert result.signature_matches[EID] == ("span-cart",)
    elif not in_run:
        assert EID not in result.signature_matches
        assert "CITATION_NOT_IN_RUN" in result.failure_reasons
        assert result.failure_class == "deterministic_gate"


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
def test_gate_missing_outcome_does_not_authorize_delivered_citations(fault):
    data = case(fault=fault)
    data["outcome"] = data["judge"] = None
    result = evaluate(**data)
    assert not result.gate.checks["citations"] and not result.overall
    assert "CITATION_NOT_IN_RUN" in result.failure_reasons
    assert result.failure_class == "execution"
    assert EID not in result.signature_matches
    assert result.gate.checks["signature"] is (not fault)


@pytest.mark.parametrize("kind", ["fact", "hypothesis", "recommendation"])
@pytest.mark.parametrize("location", ["claims[0]", "summary"])
@pytest.mark.parametrize("second_in_run", [False, True])
def test_diagnosis_cannot_borrow_foreign_citation_despite_own_signature(
    kind, location, second_in_run
):
    data = case()
    second_id = "ev-second-run-candidate"
    data["delivered"][second_id] = record(
        evidence_id=second_id,
        rows=[span(span_id="span-second")],
    )
    data["outcome"] = replace(
        data["outcome"],
        evidence_ids=(EID, second_id) if second_in_run else (EID,),
    )
    data["report"] = capture(claims=[claim(kind=kind, evidence_ids=[EID, second_id])])
    data["judge"] = judge(
        diagnoses=(
            diagnosis(
                location=location,
                linked_claim=0 if location == "summary" else None,
                cited_evidence_ids=(EID, second_id),
            ),
        )
    )
    result = evaluate(**data)
    # The own-Run signature makes the original verdict True independently of
    # the second record. Qualification must still check every cited record.
    assert result.gate.checks["signature"]
    assert result.signature_matches[EID] == ("span-cart",)
    assert result.judge_status == "ok"
    assert result.diagnosis_pass is second_in_run
    assert result.judge_pass is second_in_run and result.overall is second_in_run
    if second_in_run:
        assert result.signature_matches[second_id] == ("span-second",)
        assert result.failure_class is None and result.failure_reasons == ()
    else:
        assert second_id not in result.signature_matches
        assert "CITATION_NOT_IN_RUN" in result.failure_reasons
        assert "DIAGNOSIS_EVIDENCE_UNQUALIFIED" in result.failure_reasons
        assert result.failure_class == "deterministic_gate"


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
@pytest.mark.parametrize("kind", ["fact", "counter_evidence", "rejected_hypothesis"])
@pytest.mark.parametrize("scenario_target", [False, True])
def test_gate_rejects_matching_record_and_claim_on_non_scenario_target(
    fault, kind, scenario_target
):
    data = case(fault=fault)
    target = data["spec"].stimulus.target_id if scenario_target else "other-target"
    data["delivered"][EID] = record(target_id=target)
    data["report"] = capture(
        fault=fault,
        claims=[
            claim(
                CAUSE if fault else OBSERVATION,
                kind=kind,
                target_refs=[target],
            )
        ],
    )
    gate = run_gate(**gate_args(data))
    assert (
        gate.passed is scenario_target and gate.checks["citations"] is scenario_target
    )
    assert ("TARGET_MISMATCH" in gate.reasons) is (not scenario_target)
    assert gate.checks["signature"] is (scenario_target or not fault)
    if not scenario_target:
        data["judge"] = None
    result = evaluate(**data)
    assert result.overall is scenario_target
    if not scenario_target:
        assert EID not in result.signature_matches
        assert "TARGET_MISMATCH" in result.failure_reasons
        assert result.failure_class == "deterministic_gate"


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
@pytest.mark.parametrize(
    "start_us,matches",
    [
        (micros(START), True),
        (10**100, False),
        (-(10**100), False),
    ],
)
def test_signature_out_of_range_start_is_nonmatch_and_evaluate_returns(
    fault, start_us, matches
):
    data = case(fault=fault)
    visible = record(rows=[span(start_us=start_us)])
    assert signature_matches(rule(), visible) == (("span-cart",) if matches else ())
    data["delivered"][EID] = visible
    if fault:
        data["judge"] = judge(verdict=matches)
    result = evaluate(**data)
    expected_pass = matches or not fault
    assert result.gate.checks["citations"] and result.evidence_status == "complete"
    assert result.gate.checks["signature"] is expected_pass
    assert result.judge_status == "ok" and result.diagnosis_pass is expected_pass
    assert result.overall is expected_pass
    if not matches:
        assert not result.signature_matches.get(EID, ())
    if fault and not matches:
        assert "SIGNATURE_ABSENT" in result.failure_reasons
        assert "DIAGNOSIS_FAILED" in result.failure_reasons
        assert result.failure_class == "deterministic_gate"
    else:
        assert result.failure_class is None and result.failure_reasons == ()


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
@pytest.mark.parametrize("outcome_mode", ["provided", "none", "omitted"])
def test_judge_request_filters_run_membership_and_scenario_target(fault, outcome_mode):
    data = case(fault=fault)
    foreign_id, wrong_target_id = "ev-other-run", "ev-other-target"
    data["delivered"].update(
        {
            foreign_id: record(
                evidence_id=foreign_id, rows=[span(span_id="span-other-run")]
            ),
            wrong_target_id: record(
                evidence_id=wrong_target_id, target_id="other-target"
            ),
            "ev-undelivered": record(evidence_id="ev-undelivered", delivered=False),
            "ev-unused": record(evidence_id="ev-unused"),
        }
    )
    data["outcome"] = replace(
        data["outcome"],
        evidence_ids=(EID, wrong_target_id, "ev-undelivered", "ev-unused"),
    )
    data["report"] = capture(
        fault=fault,
        claims=[
            claim(evidence_ids=[foreign_id, EID]),
            claim(evidence_ids=[wrong_target_id], target_refs=["other-target"]),
            claim(evidence_ids=["ev-undelivered"]),
            claim(evidence_ids=[EID]),
        ],
    )
    args = {
        key: data[key]
        for key in ("spec", "answer", "report", "delivered", "authorized")
    }
    if outcome_mode != "omitted":
        args["outcome"] = data["outcome"] if outcome_mode == "provided" else None
    request = judge_request(**args)
    # interface:132 explicitly retains the delivered-key fallback when outcome
    # is absent; scenario-target and delivery filtering still apply there.
    expected_ids = [EID] if outcome_mode == "provided" else [foreign_id, EID]
    assert request["cited_evidence"] == [
        {
            "evidence_id": eid,
            "view_sha256": data["delivered"][eid].view_sha256,
            "view": data["delivered"][eid].view,
        }
        for eid in expected_ids
    ]
    assert set(request["signature_matches"]) == (set(expected_ids) if fault else set())
    if fault:
        assert request["signature_matches"][EID] == ["span-cart"]
        if outcome_mode != "provided":
            assert request["signature_matches"][foreign_id] == ["span-other-run"]


# PR #221 third review: interface:20,111-112,125-127,132,173.


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
@pytest.mark.parametrize(
    "kind",
    [
        "fact",
        "counter_evidence",
        "rejected_hypothesis",
        "hypothesis",
        "recommendation",
    ],
)
@pytest.mark.parametrize("identity_matches", [False, True])
def test_citation_record_identity_controls_gate_signature_and_diagnosis(
    fault, kind, identity_matches
):
    data = case(fault=fault)
    record_id = EID if identity_matches else "different-evidence-id"
    data["delivered"][EID] = record(evidence_id=record_id)
    # Both identities belong to this Run: only map-key/record-ID agreement
    # changes, rather than accidentally testing the Run-membership check.
    data["outcome"] = replace(
        data["outcome"], evidence_ids=(EID, "different-evidence-id")
    )
    data["report"] = capture(
        fault=fault,
        claims=[
            claim(
                CAUSE if fault else OBSERVATION,
                kind=kind,
            )
        ],
    )
    if fault:
        data["judge"] = judge(verdict=identity_matches)
    result = evaluate(**data)
    assert result.gate.checks["citations"] is identity_matches
    assert result.gate.passed is identity_matches and result.overall is identity_matches
    assert result.gate.checks["signature"] is (identity_matches or not fault)
    assert "CITATION_NOT_IN_RUN" not in result.failure_reasons
    assert "VIEW_HASH_MISMATCH" not in result.failure_reasons
    assert result.judge_status == "ok"
    if identity_matches:
        assert result.failure_class is None and result.failure_reasons == ()
        assert result.diagnosis_pass is True and result.judge_pass
        if fault:
            assert result.signature_matches[EID] == ("span-cart",)
    else:
        assert "CITATION_ID_MISMATCH" in result.gate.reasons
        assert "CITATION_ID_MISMATCH" in result.failure_reasons
        assert (
            EID not in result.signature_matches
            and record_id not in result.signature_matches
        )
        assert result.failure_class == "deterministic_gate"
        if fault:
            assert "SIGNATURE_ABSENT" in result.failure_reasons
            assert "DIAGNOSIS_EVIDENCE_UNQUALIFIED" in result.failure_reasons
            assert result.diagnosis_pass is False and not result.judge_pass


@pytest.mark.parametrize("fault", [False, True], ids=["normal", "fault"])
@pytest.mark.parametrize(
    "query_window,span_start,qualified",
    [
        (Window(START, END), START, True),
        (Window(START, START), START, True),
        (
            Window(START - timedelta(microseconds=1), END),
            START - timedelta(microseconds=1),
            False,
        ),
        (
            Window(START, END + timedelta(microseconds=1)),
            END + timedelta(microseconds=1),
            False,
        ),
    ],
)
def test_hypothesis_window_qualification_controls_gate_and_judge_request_signatures(
    fault, query_window, span_start, qualified
):
    data = case(fault=fault)
    data["delivered"][EID] = record(
        window=query_window,
        rows=[span(start_us=micros(span_start))],
    )
    data["report"] = capture(
        fault=fault,
        claims=[
            claim(
                CAUSE if fault else OBSERVATION,
                kind="hypothesis",
            )
        ],
    )
    if fault:
        data["judge"] = judge(verdict=qualified)
    # Row matching only knows the query window; the gate must additionally
    # require that query window to fit inside the authorized window.
    assert signature_matches(rule(), data["delivered"][EID]) == ("span-cart",)
    result = evaluate(**data)
    assert result.gate.checks["citations"]
    assert result.gate.checks["signature"] is (qualified or not fault)
    assert result.overall is (qualified or not fault)
    request = judge_request(
        **{
            key: data[key]
            for key in (
                "spec",
                "answer",
                "report",
                "delivered",
                "authorized",
                "outcome",
            )
        }
    )
    if fault and qualified:
        assert result.signature_matches[EID] == ("span-cart",)
        assert request["signature_matches"][EID] == ["span-cart"]
    else:
        assert EID not in result.signature_matches
        assert EID not in request["signature_matches"]
    if fault and not qualified:
        assert "SIGNATURE_ABSENT" in result.gate.reasons
        assert "DIAGNOSIS_EVIDENCE_UNQUALIFIED" in result.failure_reasons
        assert result.failure_class == "deterministic_gate"
        assert result.judge_status == "ok" and result.diagnosis_pass is False


@pytest.mark.parametrize("good_first", [False, True])
@pytest.mark.parametrize("diagnosis_on_good_claim", [False, True])
def test_signature_accepts_any_qualified_claim_without_lending_its_diagnosis_anchor(
    good_first, diagnosis_on_good_claim
):
    data = case()
    good = claim(kind="hypothesis")
    bad = claim(kind="hypothesis", target_refs=["other-target"])
    data["report"] = capture(claims=[good, bad] if good_first else [bad, good])
    good_index = 0 if good_first else 1
    anchor = good_index if diagnosis_on_good_claim else 1 - good_index
    data["judge"] = judge(diagnoses=(diagnosis(location=f"claims[{anchor}]"),))
    result = evaluate(**data)
    assert result.gate.passed and result.deterministic_pass
    assert result.signature_matches[EID] == ("span-cart",)
    assert result.judge_status == "ok"
    assert result.diagnosis_pass is diagnosis_on_good_claim
    assert result.overall is diagnosis_on_good_claim
    if diagnosis_on_good_claim:
        assert result.failure_class is None and result.failure_reasons == ()
    else:
        assert result.failure_class == "diagnosis"
        assert "DIAGNOSIS_EVIDENCE_UNQUALIFIED" in result.failure_reasons


@pytest.mark.parametrize("endpoint", ["start", "end", "both"])
@pytest.mark.parametrize(
    "tz",
    [None, timezone(timedelta(hours=8)), timezone(timedelta(hours=-7))],
    ids=["naive", "utc-plus-eight", "utc-minus-seven"],
)
def test_window_rejects_naive_or_non_utc_endpoints(endpoint, tz):
    start, end = START, END
    if endpoint in {"start", "both"}:
        start = START.replace(tzinfo=None) if tz is None else START.astimezone(tz)
    if endpoint in {"end", "both"}:
        end = END.replace(tzinfo=None) if tz is None else END.astimezone(tz)
    with pytest.raises(ValueError, match="^WINDOW_NOT_UTC$"):
        Window(start, end)


@pytest.mark.parametrize("delta_us", [-1, 0, 1])
def test_window_utc_ordering_accepts_equal_endpoints_and_rejects_reversal(delta_us):
    end = START + timedelta(microseconds=delta_us)
    if delta_us < 0:
        with pytest.raises(ValueError, match="^WINDOW_REVERSED$"):
            Window(START, end)
    else:
        window = Window(START, end)
        assert window.start == START and window.end == end
