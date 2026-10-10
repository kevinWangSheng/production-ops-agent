"""Implementer unit tests for the Alertmanager intake (M1-04 step 2, no PG).

Parsing and identity normalization, target matching, the deterministic
question, the redacted bounded record, the versioned Run input and the
endpoint's refusals before any store is touched.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opspilot import alertmanager as am
from opspilot import schema
from opspilot.investigation.context import ContextError, InvestigationInput
from opspilot.investigation.inputs import continuation_input
from opspilot.tools.fixture import fixture_face
from opspilot.tools.registry import canonical_hash
from opspilot.web.store import MappingTargetRegistry
from tests.m1_web_support import basic, bearer, build_workbench, call, post_json

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence/m1-04-lab"
IDENTITY = {
    "integration_id": "otel",
    "cluster_uid": "kind",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
}


def _lab_alert() -> dict:
    return json.loads((EVIDENCE / "webhook-firing.json").read_text())["alerts"][0]


# -- parsing and identity ----------------------------------------------


def test_lab_alert_parses_with_utc_second_identity():
    alert = am.parse_alert(_lab_alert())
    assert alert.status == "firing"
    assert alert.fingerprint == "83f7541ae0105d42"
    assert alert.starts_at == "2026-10-10T10:41:54Z"
    assert alert.starts_at_raw == "2026-10-10T10:41:54.365Z"
    assert alert.delivery_key == am.delivery_key(
        source="alertmanager", external_event_id="83f7541ae0105d42:2026-10-10T10:41:54Z"
    )


def test_offsets_normalize_to_the_same_identity():
    base = _lab_alert()
    same = am.parse_alert({**base, "startsAt": "2026-10-10T12:41:54.9+02:00"})
    assert same.starts_at == "2026-10-10T10:41:54Z"
    assert same.delivery_key == am.parse_alert(base).delivery_key
    later = am.parse_alert({**base, "startsAt": "2026-10-10T10:41:55Z"})
    assert later.delivery_key != same.delivery_key


@pytest.mark.parametrize(
    "change,code",
    [
        ({"fingerprint": None}, "FINGERPRINT_MISSING"),
        ({"fingerprint": ""}, "FINGERPRINT_MISSING"),
        ({"fingerprint": "a b"}, "FINGERPRINT_INVALID"),
        ({"fingerprint": "f" * 257}, "FINGERPRINT_INVALID"),
        ({"startsAt": None}, "STARTS_AT_MISSING"),
        ({"startsAt": "2026-10-10T10:41:54"}, "STARTS_AT_INVALID"),
        ({"startsAt": "2026-10-10"}, "STARTS_AT_INVALID"),
        ({"startsAt": "yesterday"}, "STARTS_AT_INVALID"),
        ({"labels": None}, "LABELS_MISSING"),
        ({"labels": {}}, "LABELS_MISSING"),
        ({"labels": {"a": 1}}, "LABELS_INVALID"),
        ({"annotations": ["x"]}, "ANNOTATIONS_INVALID"),
        ({"status": "pending"}, "STATUS_INVALID"),
        ({"fingerprint": "\ud800"}, "ALERT_NOT_ENCODABLE"),
    ],
)
def test_unidentifiable_alerts_are_invalid(change, code):
    item = {**_lab_alert(), **change}
    with pytest.raises(am.InvalidAlert) as refused:
        am.parse_alert(item)
    assert refused.value.code == code


def test_missing_annotations_are_empty():
    item = _lab_alert()
    del item["annotations"]
    assert am.parse_alert(item).annotations == {}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "x",
        {"version": "3", "alerts": []},
        {"version": 4, "alerts": []},
        {"version": "4"},
    ],
)
def test_payload_shape_is_refused(payload):
    with pytest.raises(am.InvalidPayload):
        am.parse_payload(payload)


# -- target match (I4) --------------------------------------------------


def _registry() -> MappingTargetRegistry:
    return MappingTargetRegistry(
        {
            "checkout-prod": {
                **IDENTITY,
                "match": {"version": 1, "labels": {"service": "checkout"}},
            },
            "checkout-canary": {
                **IDENTITY,
                "match": {
                    "version": 1,
                    "labels": {"service": "checkout", "track": "canary"},
                },
            },
            "payment-prod": dict(IDENTITY),
        }
    )


def test_exactly_one_match_binds_zero_or_many_hand_off():
    registry = _registry()
    one = am.resolve_target(registry.match_alert({"service": "checkout"}))
    assert (one.target_id, one.namespace, one.workload, one.handoff_reason) == (
        "checkout-prod",
        "otel-demo",
        "checkout",
        None,
    )
    none = am.resolve_target(registry.match_alert({"service": "payment"}))
    assert (none.target_id, none.handoff_reason) == (None, "TARGET_UNRESOLVED")
    many = am.resolve_target(
        registry.match_alert({"service": "checkout", "track": "canary"})
    )
    assert (many.target_id, many.handoff_reason) == (None, "TARGET_AMBIGUOUS")


@pytest.mark.parametrize(
    "match",
    [
        {"version": 2, "labels": {"a": "b"}},
        {"version": "1", "labels": {"a": "b"}},
        {"version": 1, "labels": {}},
        {"version": 1, "labels": {"a": ""}},
        {"version": 1, "labels": {"": "b"}},
        {"version": 1, "labels": ["a"]},
        {"version": 1, "labels": {"a": "b"}, "extra": 1},
        {"labels": {"a": "b"}},
        "service=checkout",
    ],
)
def test_malformed_match_is_refused_at_load(tmp_path, match):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"checkout-prod": {**IDENTITY, "match": match}}))
    with pytest.raises(schema.TargetIdentityMissing) as refused:
        schema.load_target_identities(path)
    assert refused.value.resource_uids == ["checkout-prod"]
    with pytest.raises(ValueError):
        MappingTargetRegistry({"checkout-prod": {**IDENTITY, "match": match}})


def test_valid_match_loads_and_entries_without_it_stay_unchanged(tmp_path):
    path = tmp_path / "identities.json"
    match = {"version": 1, "labels": {"service": "checkout"}}
    path.write_text(
        json.dumps({"checkout-prod": {**IDENTITY, "match": match}, "x": IDENTITY})
    )
    loaded = schema.load_target_identities(path)
    assert loaded == {"checkout-prod": {**IDENTITY, "match": match}, "x": IDENTITY}
    registry = MappingTargetRegistry.from_file(str(path))
    assert [t.resource_uid for t in registry.match_alert({"service": "checkout"})] == [
        "checkout-prod"
    ]


# -- question (I6) --------------------------------------------------------


def _resolution():
    return am.Resolution("checkout-prod", "otel-demo", "checkout", None)


def test_question_uses_labels_target_time_and_promql_only():
    alert = am.parse_alert(_lab_alert())
    question = am.question_for(alert, _resolution())
    assert question == am.question_for(am.parse_alert(_lab_alert()), _resolution())
    assert question.startswith(
        "Alertmanager alert CheckoutPlaceOrderErrorRatioHigh (severity critical) "
        "is firing for service otel-demo/checkout since 2026-10-10T10:41:54Z."
    )
    assert 'status_code="STATUS_CODE_ERROR"' in question
    for annotation in alert.annotations.values():
        assert annotation not in question


def test_question_bounds_fields_and_promql_and_marks_the_cut():
    item = _lab_alert()
    item["labels"] = {**item["labels"], "alertname": "A" * 300 + "‮\x00"}
    item["generatorURL"] = "http://p/graph?g0.expr=" + "x" * 3000
    question = am.question_for(am.parse_alert(item), _resolution())
    assert "A" * 256 + am.TRUNCATED in question
    assert "x" * 2048 + am.TRUNCATED in question
    assert "\x00" not in question and "‮" not in question


def test_question_omits_an_unparsable_expression():
    item = {**_lab_alert(), "generatorURL": "not a url"}
    question = am.question_for(am.parse_alert(item), _resolution())
    assert "PromQL" not in question


# -- audit record (I7) ----------------------------------------------------


def test_record_redacts_bounds_and_hashes_the_raw_alert():
    item = _lab_alert()
    item["annotations"] = {
        "api_key": "abc123",
        "runbook": "https://user:pw@wiki.example/run",
        "description": "d" * 20000,
    }
    record = am.record_of(am.parse_alert(item))
    assert record.truncated is True
    assert len(record.alert_json.encode()) <= am.MAX_ALERT_RECORD_BYTES
    assert "abc123" not in record.alert_json and "user:pw" not in record.alert_json
    assert record.raw_sha256 == canonical_hash(item)
    small = am.record_of(am.parse_alert(_lab_alert()))
    assert small.truncated is False
    stored = json.loads(small.alert_json)
    assert stored["labels"]["alertname"] == "CheckoutPlaceOrderErrorRatioHigh"
    assert stored["annotations"] == _lab_alert()["annotations"]


# -- Run input (E11) ------------------------------------------------------


def _input(**facts) -> InvestigationInput:
    fresh = fixture_face().input_for(
        run_id="run-1",
        question="q",
        target_id="checkout-prod",
        deadline=datetime(2026, 10, 10, tzinfo=UTC),
        model_requests=2,
    )
    return replace(fresh, scope_facts={**fresh.scope_facts, **facts})


def test_affected_service_bumps_the_input_version_and_round_trips():
    plain = _input().as_json()
    assert plain["version"] == "opspilot-investigation-input-v1"
    assert InvestigationInput.from_json(plain) == _input()
    service = {"namespace": "otel-demo", "workload": "checkout"}
    focused = _input(affected_service=service)
    raw = focused.as_json()
    assert raw["version"] == "opspilot-investigation-input-v2"
    assert InvestigationInput.from_json(raw) == focused
    # A v1 row carrying the fact, or a v2 row without it, is refused.
    with pytest.raises(ContextError):
        InvestigationInput.from_json(
            {**raw, "version": "opspilot-investigation-input-v1"}
        )
    with pytest.raises(ContextError):
        InvestigationInput.from_json(
            {**plain, "version": "opspilot-investigation-input-v2"}
        )
    with pytest.raises(ContextError):
        _input(affected_service={"namespace": "otel-demo"})


def test_a_successor_run_keeps_the_affected_service():
    service = {"namespace": "otel-demo", "workload": "checkout"}
    snapshot = {
        "run": {"run_id": "run-1", "input": _input(affected_service=service).as_json()},
        "steps": [],
    }
    successor = continuation_input(
        snapshot,
        new_run_id="run-2",
        deadline=datetime(2026, 10, 11, tzinfo=UTC),
        authorized_targets=frozenset({"checkout-prod"}),
    )
    assert successor.scope_facts["affected_service"] == service
    assert successor.as_json()["version"] == "opspilot-investigation-input-v2"


# -- endpoint refusals (no store reached) ---------------------------------


def test_endpoint_refuses_bad_auth_and_bad_payloads_before_the_store():
    app, workbench, _ = build_workbench()
    body = {"version": "4", "alerts": []}
    assert post_json(app, "/intake/alertmanager", body, headers={}).status == 401
    assert post_json(app, "/intake/alertmanager", body, headers=basic()).status == 401
    empty = post_json(app, "/intake/alertmanager", body, headers=bearer())
    assert empty.status == 200 and empty.json() == {"results": []}
    for bad in (b"{", b"[]", b'{"version":"4","alerts":null}'):
        refused = call(
            app,
            "POST",
            "/intake/alertmanager",
            headers={**bearer(), "content-type": "application/json"},
            body=bad,
        )
        assert refused.status == 400
        assert refused.json() == {"error": "INVALID_ALERTMANAGER_PAYLOAD"}
    invalid = post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "alerts": [{"status": "firing"}]},
        headers=bearer(),
    )
    assert invalid.status == 200
    assert invalid.json()["results"][0]["outcome"] == "invalid"
    assert workbench.list_incidents() == ()


def test_migration_vocabulary_matches_the_code():
    from importlib import import_module

    migration = import_module("opspilot.migrations.versions.0010_alert_intake")
    assert migration.HANDOFF_REASONS == ("TARGET_UNRESOLVED", "TARGET_AMBIGUOUS")
    assert set(migration.OUTCOMES) == {
        "created",
        "replayed",
        "handoff_created",
        "handoff_replayed",
        "resolved_attached",
        "resolved_recorded",
    }


# -- R12: an input version this build does not know blocks as INCOMPATIBLE_STATE


def test_an_unknown_input_version_is_incompatible_a_malformed_one_invalid(monkeypatch):
    from opspilot.investigation import context

    service = {"namespace": "otel-demo", "workload": "checkout"}
    v2 = _input(affected_service=service).as_json()
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        InvestigationInput.from_json(
            {**v2, "version": "opspilot-investigation-input-v5"}
        )
    for bad in ("bogus", None, 2, "opspilot-investigation-input-v0"):
        with pytest.raises(ContextError, match="INPUT_INVALID"):
            InvestigationInput.from_json({**v2, "version": bad})
    # A worker built before v2 existed reads a v2 row.
    monkeypatch.setattr(context, "KNOWN_INPUT_VERSIONS", (context.INPUT_VERSION,))
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        InvestigationInput.from_json(v2)
