"""One entry alert per scenario -> an Alertmanager webhook v4 body (#581).

Selection rule (task record, r4; same as the 2026-11-04 fault-start proxy):
take the last alert file, preferring files named ``*alerting_state*``; drop
baseline alerts (``BASELINE_ALERTNAMES``) and entries that are not firing or
have no ``activeAt``; the entry alert is the one with the earliest
``activeAt``, ties broken by its sorted labels.

The webhook carries exactly that alert with ``startsAt = activeAt + offset``
and a fingerprint derived only from its labels. The target entry is scenario
configuration (``EntryTarget``): the chosen alert must carry its match labels,
otherwise the intake could not bind it to a target and the conversion fails.
The product intake (``opspilot.alertmanager``) is read, never changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.itbench_lite.shift import is_rfc3339, parse_rfc3339_ns, shift_rfc3339
from scripts.itbench_lite.snapshot import Snapshot, load_snapshot
from scripts.itbench_lite.traces import post_json_loopback

#: Alerts that fire in every scenario regardless of the fault.
BASELINE_ALERTNAMES = frozenset(
    {
        "Watchdog",
        "InfoInhibitor",
        "KubeClientCertificateExpiration",
        "TargetDown",
        "KubeSchedulerDown",
        "KubeControllerManagerDown",
        "KubeProxyDown",
        "NodeClockNotSynchronising",
        "etcdInsufficientMembers",
        "etcdMembersDown",
        "AlertmanagerFailedToSendAlerts",
        "CPUThrottlingHigh",
        "KubeletDown",
    }
)
FINAL_FILE_MARKER = "alerting_state"
INTAKE_PATH = "/intake/alertmanager"
ZERO_TIME = "0001-01-01T00:00:00Z"
RECEIVER = "opspilot"
#: An attempt is void when delivery comes later than this after loading (r4).
MAX_DELIVERY_GAP = timedelta(minutes=2)


class NoEntryAlert(LookupError):
    """The scenario's final alert file has no firing non-baseline alert."""


@dataclass(frozen=True)
class EntryTarget:
    """Scenario configuration: the labels the target entry's ``match`` uses."""

    match_labels: Mapping[str, str] = field(default_factory=dict)


def final_alert_file(snapshot: Snapshot) -> Path:
    if not snapshot.alerts:
        raise NoEntryAlert("scenario has no alert file")
    marked = [p for p in snapshot.alerts if FINAL_FILE_MARKER in p.name]
    return sorted(marked or snapshot.alerts, key=lambda p: (p.name, str(p)))[-1]


def _labels(entry: Mapping[str, Any]) -> dict[str, str]:
    labels = entry.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise ValueError("alert has no labels")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        raise ValueError("alert labels must be strings")
    return dict(labels)


def _canonical_labels(labels: Mapping[str, str]) -> str:
    return json.dumps(sorted(labels.items()), ensure_ascii=False, separators=(",", ":"))


def select_entry_alert(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The earliest firing non-baseline alert of one file (rule in module doc)."""
    candidates = []
    for entry in entries:
        labels = entry.get("labels")
        if not isinstance(labels, dict):
            continue
        if labels.get("alertname") in BASELINE_ALERTNAMES:
            continue
        if entry.get("state") != "firing" or not is_rfc3339(entry.get("activeAt")):
            continue
        candidates.append(entry)
    if not candidates:
        raise NoEntryAlert("no firing non-baseline alert")
    chosen = min(
        candidates,
        key=lambda e: (
            parse_rfc3339_ns(e["activeAt"]),
            _canonical_labels(_labels(e)),
        ),
    )
    return dict(chosen)


def fingerprint(labels: Mapping[str, str]) -> str:
    """First 16 hex digits of sha256 over the sorted labels (deterministic)."""
    return hashlib.sha256(_canonical_labels(labels).encode()).hexdigest()[:16]


def default_match_labels(alert: Mapping[str, Any]) -> dict[str, str]:
    """The r4 default target match for an entry alert: its ``service_name``."""
    service = _labels(alert).get("service_name")
    if not service:
        raise ValueError("entry alert has no service_name label")
    return {"service_name": service}


def build_webhook(
    alert: Mapping[str, Any], *, offset_seconds: int, target: EntryTarget
) -> dict[str, Any]:
    """The Alertmanager webhook v4 body carrying ``alert`` alone, firing."""
    labels = _labels(alert)
    if not target.match_labels:
        raise ValueError("target entry has no match labels")
    missing = {k: v for k, v in target.match_labels.items() if labels.get(k) != v}
    if missing:
        raise ValueError(
            "entry alert does not carry the target's match labels: "
            + ", ".join(sorted(missing))
        )
    raw_annotations = alert.get("annotations") or {}
    if not isinstance(raw_annotations, dict):
        raise ValueError("alert annotations must be an object")
    annotations = {str(k): str(v) for k, v in raw_annotations.items()}
    active = alert.get("activeAt")
    if not is_rfc3339(active):
        raise ValueError("alert has no RFC 3339 activeAt")
    name = labels.get("alertname", "")
    item = {
        "status": "firing",
        "labels": labels,
        "annotations": annotations,
        "startsAt": shift_rfc3339(active, offset_seconds),
        "endsAt": ZERO_TIME,
        "fingerprint": fingerprint(labels),
    }
    return {
        "version": "4",
        "groupKey": '{}:{alertname="%s"}' % name,
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": RECEIVER,
        "groupLabels": {"alertname": name},
        "commonLabels": labels,
        "commonAnnotations": annotations,
        "externalURL": "http://127.0.0.1:9093",
        "alerts": [item],
    }


def entry_webhook(
    snapshot: Snapshot, *, offset_seconds: int, target: EntryTarget
) -> dict[str, Any]:
    """Select the scenario's entry alert and convert it."""
    entries = snapshot.read_alert_file(final_alert_file(snapshot))
    return build_webhook(
        select_entry_alert(entries), offset_seconds=offset_seconds, target=target
    )


def delivery_gap_ok(
    loaded_at: datetime, delivered_at: datetime, *, limit: timedelta = MAX_DELIVERY_GAP
) -> bool:
    """Whether delivery came soon enough after loading for the attempt to count."""
    gap = delivered_at - loaded_at
    return timedelta(0) <= gap <= limit


def post_webhook(base_url: str, token: str, body: Mapping[str, Any]) -> int:
    """Deliver ``body`` to the intake at a loopback ``base_url``; HTTP status."""
    return post_json_loopback(
        base_url.rstrip("/") + INTAKE_PATH,
        body,
        headers={"Authorization": f"Bearer {token}"},
    )


def _match(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError("expected label=value")
    return key, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot")
    parser.add_argument("--offset-seconds", type=int, required=True)
    parser.add_argument(
        "--match",
        type=_match,
        action="append",
        required=True,
        help="target match label, label=value (repeatable)",
    )
    args = parser.parse_args(argv)
    snapshot = load_snapshot(args.snapshot)
    body = entry_webhook(
        snapshot,
        offset_seconds=args.offset_seconds,
        target=EntryTarget(match_labels=dict(args.match)),
    )
    json.dump(body, sys.stdout, indent=1, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
