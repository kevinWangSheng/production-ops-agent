"""#581: one entry alert per scenario, converted into a webhook the existing
intake accepts, with a deterministic fingerprint. Synthetic alerts only; the
product intake is imported read-only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opspilot import alertmanager as am
from opspilot.web.store import MappingTargetRegistry
from scripts.itbench_lite import alerts
from scripts.itbench_lite.alerts import EntryTarget, NoEntryAlert
from scripts.itbench_lite.snapshot import load_snapshot

IDENTITY = {
    "integration_id": "otel",
    "cluster_uid": "kind",
    "namespace": "otel-demo",
    "workload": "frontend",
    "health_profile_id": "otel-demo-frontend",
}


def _alert(name: str, active: str, state: str = "firing", **labels: str) -> dict:
    return {
        "labels": {"alertname": name, "severity": "warning", **labels},
        "annotations": {"summary": f"{name} is high"},
        "state": state,
        "activeAt": active,
        "value": "1e+00",
    }


def _scenario(tmp_path: Path, files: dict[str, list[dict]]) -> Path:
    root = tmp_path / "s"
    (root / "alerts").mkdir(parents=True)
    for name, entries in files.items():
        (root / "alerts" / name).write_text(
            json.dumps({"status": "success", "data": {"alerts": entries}})
        )
    return root


FINAL = [
    _alert("Watchdog", "2025-12-15T17:00:00Z", severity="none"),
    _alert(
        "RequestLatency", "2025-12-15T17:40:00Z", state="pending", service_name="cart"
    ),
    _alert(
        "RequestErrorRate", "2025-12-15T17:47:32.123456789Z", service_name="frontend"
    ),
    _alert("RequestErrorRate", "2025-12-15T17:49:00Z", service_name="checkout"),
]


def _snap(tmp_path):
    return load_snapshot(
        _scenario(
            tmp_path,
            {
                "alerts_at_2025-12-15T17-30-00.json": [
                    _alert("Early", "2025-12-15T17:20:00Z", service_name="ad")
                ],
                "alerts_in_alerting_state_2025-12-15T175000Z.json": [
                    _alert("Older", "2025-12-15T17:21:00Z", service_name="ad")
                ],
                "alerts_in_alerting_state_2025-12-15T175500Z.json": FINAL,
            },
        )
    )


def test_entry_alert_is_the_earliest_firing_non_baseline_alert_of_the_last_file(
    tmp_path,
):
    snap = _snap(tmp_path)
    assert (
        alerts.final_alert_file(snap).name
        == "alerts_in_alerting_state_2025-12-15T175500Z.json"
    )
    chosen = alerts.select_entry_alert(
        snap.read_alert_file(alerts.final_alert_file(snap))
    )
    assert chosen["labels"]["service_name"] == "frontend"
    assert alerts.default_match_labels(chosen) == {"service_name": "frontend"}


def test_alerting_state_files_win_over_later_plain_snapshots(tmp_path):
    snap = load_snapshot(
        _scenario(
            tmp_path,
            {
                "alerts_in_alerting_state_1.json": FINAL,
                "alerts_zz_later.json": [
                    _alert("Other", "2025-12-15T17:00:00Z", service_name="ad")
                ],
            },
        )
    )
    assert alerts.final_alert_file(snap).name == "alerts_in_alerting_state_1.json"


def test_ties_break_on_labels_deterministically():
    a = _alert("RequestErrorRate", "2025-12-15T17:47:00Z", service_name="b")
    b = _alert("RequestErrorRate", "2025-12-15T17:47:00.000Z", service_name="a")
    assert alerts.select_entry_alert([a, b])["labels"]["service_name"] == "a"
    assert alerts.select_entry_alert([b, a])["labels"]["service_name"] == "a"


def test_no_candidate_raises():
    with pytest.raises(NoEntryAlert):
        alerts.select_entry_alert(
            [
                _alert("Watchdog", "2025-12-15T17:00:00Z"),
                _alert("X", "2025-12-15T17:00:00Z", state="pending"),
            ]
        )


def test_fingerprint_is_deterministic_and_label_order_free():
    one = alerts.fingerprint({"alertname": "A", "service_name": "x"})
    two = alerts.fingerprint({"service_name": "x", "alertname": "A"})
    assert one == two and len(one) == 16 and int(one, 16) >= 0
    assert one != alerts.fingerprint({"alertname": "A", "service_name": "y"})


def test_webhook_is_accepted_by_the_existing_intake_and_binds_one_target(tmp_path):
    snap = _snap(tmp_path)
    target = EntryTarget(match_labels={"service_name": "frontend"})
    body = alerts.entry_webhook(snap, offset_seconds=86400, target=target)
    assert body == alerts.entry_webhook(snap, offset_seconds=86400, target=target)
    items = am.parse_payload(json.loads(json.dumps(body)))
    assert len(items) == 1
    alert = am.parse_alert(items[0])
    assert alert.status == "firing"
    assert alert.starts_at == "2025-12-16T17:47:32Z"
    assert alert.starts_at_raw == "2025-12-16T17:47:32.123456789Z"
    assert alert.fingerprint == alerts.fingerprint(alert.labels)
    assert alert.labels["alertname"] == "RequestErrorRate"
    registry = MappingTargetRegistry(
        {
            "frontend": {
                **IDENTITY,
                "match": {"version": 1, "labels": dict(target.match_labels)},
            },
            "checkout": {
                **IDENTITY,
                "workload": "checkout",
                "match": {"version": 1, "labels": {"service_name": "checkout"}},
            },
        }
    )
    resolution = am.resolve_target(registry.match_alert(alert.labels))
    assert (resolution.target_id, resolution.handoff_reason) == ("frontend", None)


def test_target_match_labels_must_be_carried_by_the_entry_alert(tmp_path):
    snap = _snap(tmp_path)
    with pytest.raises(ValueError, match="service_name"):
        alerts.entry_webhook(
            snap, offset_seconds=0, target=EntryTarget({"service_name": "checkout"})
        )
    with pytest.raises(ValueError):
        alerts.entry_webhook(snap, offset_seconds=0, target=EntryTarget({}))


def test_delivery_gap_rule():
    loaded = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    assert alerts.delivery_gap_ok(loaded, loaded + timedelta(minutes=2))
    assert not alerts.delivery_gap_ok(loaded, loaded + timedelta(minutes=2, seconds=1))
    assert not alerts.delivery_gap_ok(loaded, loaded - timedelta(seconds=1))


def test_post_webhook_refuses_non_loopback():
    with pytest.raises(ValueError):
        alerts.post_webhook(
            "http://10.1.2.3:8080", "aaaaaaaa357", {"version": "4", "alerts": []}
        )
