"""The identity file (``OPSPILOT_TARGET_IDENTITIES``) is validated at load.

Every entry must carry the three identity columns, the workload and the
profile id as non-empty strings; an entry that could never register a
remediation is refused when the file is read, naming the entry, instead of
failing on every registration later (PR #120 bot review, round 3).
"""

import json
from pathlib import Path

import pytest

from opspilot import schema
from opspilot.web.store import MappingTargetRegistry, TargetIdentity

ENTRY = {
    "integration_id": "otel",
    "cluster_uid": "kind",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
}


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "identities.json"
    path.write_text(json.dumps(payload))
    return path


def test_a_complete_entry_loads_and_resolves(tmp_path: Path) -> None:
    path = _write(
        tmp_path, {"checkout-prod": {**ENTRY, "resource_uid": "checkout-prod"}}
    )
    assert schema.load_target_identities(path) == {"checkout-prod": ENTRY}
    registry = MappingTargetRegistry.from_file(str(path))
    assert registry.resolve("checkout-prod") == TargetIdentity(
        resource_uid="checkout-prod", **ENTRY
    )
    assert registry.resolve("checkout-canary") is None


@pytest.mark.parametrize("field", sorted(ENTRY))
def test_a_missing_or_empty_field_refuses_the_file_naming_the_entry(
    tmp_path: Path, field: str
) -> None:
    for broken in (
        {k: v for k, v in ENTRY.items() if k != field},
        {**ENTRY, field: ""},
    ):
        path = _write(tmp_path, {"ok": ENTRY, "payment-prod": broken})
        with pytest.raises(schema.TargetIdentityMissing) as refused:
            schema.load_target_identities(path)
        assert refused.value.resource_uids == ["payment-prod"]
        assert field in str(refused.value)


@pytest.mark.parametrize(
    "payload,detail",
    [
        ({"x": {**ENTRY, "extra": "y"}}, "unknown key"),
        ({"x": {**ENTRY, "resource_uid": "other"}}, "resource_uid differs"),
        ({"x": "not-an-object"}, "malformed"),
        (["list"], "not an object"),
    ],
)
def test_other_malformed_files_are_refused(
    tmp_path: Path, payload: object, detail: str
) -> None:
    with pytest.raises(schema.TargetIdentityMissing, match=detail):
        schema.load_target_identities(_write(tmp_path, payload))


def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(schema.TargetIdentityMissing, match="unreadable"):
        schema.load_target_identities(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(schema.TargetIdentityMissing, match="unreadable"):
        schema.load_target_identities(bad)
