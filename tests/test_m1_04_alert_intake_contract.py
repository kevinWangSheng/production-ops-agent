"""M1-04 r3 I1/I4/I10 contracts; independent author, no alert implementation read.

Contract gaps covered by r4 in the PG acceptance module: I3 normalization and I6
question properties are observable through the submitted I8 run_input; these
contract tests remain focused on no-PG registry and envelope boundaries.
E6 calls the format match_labels; r3 I4 concretizes it as match (used here).
I2 requires 200 even for created, unlike the existing /intake/events 201.
No assertion fixes question wording, unspecified reason codes or JSON hashing
serialization. Refusal exceptions follow the existing identity-file contract.
"""

import json

import pytest

from opspilot import schema
from tests.m1_web_support import basic, bearer, build_workbench, call, post_json

ENTRY = {
    "integration_id": "otel",
    "cluster_uid": "kind",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
}
MATCH = {"version": 1, "labels": {"namespace": "otel-demo", "service": "checkout"}}


def write_registry(tmp_path, entry):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"checkout-prod": entry}), encoding="utf-8")
    return path


def test_i4_versioned_match_loads_and_preserves_labels(tmp_path):
    entry = {**ENTRY, "match": MATCH}
    assert schema.load_target_identities(write_registry(tmp_path, entry)) == {
        "checkout-prod": entry
    }


def test_i4_legacy_registry_without_match_still_loads(tmp_path):
    assert schema.load_target_identities(write_registry(tmp_path, ENTRY)) == {
        "checkout-prod": ENTRY
    }


@pytest.mark.parametrize(
    "match",
    [
        {"version": 2, "labels": {"service": "checkout"}},
        {"version": 1, "labels": {}},
        {"version": 1, "labels": {"service": 12}},
        {"version": 1, "labels": {"service": ""}},
        {"version": 1, "labels": {"": "checkout"}},
        {"version": 1, "labels": {"service": "checkout"}, "extra": True},
        {"version": 1},
        {"labels": {"service": "checkout"}},
        None,
        [],
    ],
    ids=[
        "version",
        "empty",
        "number",
        "empty-value",
        "empty-key",
        "unknown",
        "missing-labels",
        "missing-version",
        "null",
        "list",
    ],
)
def test_i4_invalid_match_is_rejected_at_load(tmp_path, match):
    with pytest.raises(schema.TargetIdentityMissing):
        schema.load_target_identities(
            write_registry(tmp_path, {**ENTRY, "match": match})
        )


def test_i4_unknown_entry_key_is_rejected(tmp_path):
    with pytest.raises(schema.TargetIdentityMissing):
        schema.load_target_identities(
            write_registry(tmp_path, {**ENTRY, "match": MATCH, "unexpected": "x"})
        )


@pytest.mark.parametrize(
    "headers",
    [basic(), bearer("unknown-alert-token")],
    ids=["ui-basic", "unknown-bearer"],
)
def test_i1_i10_alert_endpoint_rejects_other_authentication(headers):
    app, _, _ = build_workbench()
    before = call(app, "GET", "/", headers=basic()).text
    response = post_json(
        app, "/intake/alertmanager", {"version": "4", "alerts": []}, headers=headers
    )
    assert response.status == 401
    assert call(app, "GET", "/", headers=basic()).text == before


@pytest.mark.parametrize(
    "payload",
    [
        {"version": "3", "alerts": []},
        {"version": 4, "alerts": []},
        {"version": "4", "alerts": {}},
        {"version": "4", "alerts": None},
        {"version": "4"},
        [],
        None,
    ],
    ids=[
        "old-version",
        "numeric-version",
        "object-alerts",
        "null-alerts",
        "missing-alerts",
        "list-body",
        "null-body",
    ],
)
def test_i1_invalid_envelope_is_400_without_visible_incidents(payload):
    app, _, _ = build_workbench()
    before = call(app, "GET", "/", headers=basic()).text
    response = post_json(app, "/intake/alertmanager", payload, headers=bearer())
    assert response.status == 400
    assert response.json() == {"error": "INVALID_ALERTMANAGER_PAYLOAD"}
    assert call(app, "GET", "/", headers=basic()).text == before


def test_i1_unparseable_json_is_400():
    app, _, _ = build_workbench()
    response = call(
        app,
        "POST",
        "/intake/alertmanager",
        headers={**bearer(), "content-type": "application/json"},
        body=b"{broken",
    )
    assert response.status == 400
    assert response.json() == {"error": "INVALID_ALERTMANAGER_PAYLOAD"}
