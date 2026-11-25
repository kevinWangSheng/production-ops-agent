"""Entry alert of a live scenario -> Alertmanager webhook v4 (#564 A564-1).

ITBench's ``kube-prometheus-stack`` release deploys no Alertmanager
(upstream ``project/roles/tools/tasks/install_prometheus.yaml``:
``alertmanager.enabled: false``), so the primary path of A564-1 (an
Alertmanager receiver pointing at the intake) has nothing to configure.
The fallback of A564-1 applies, the ITBench-Lite way: the harness reads
Prometheus' own ``/api/v1/alerts`` (the same shape as Lite's alert files),
and forwards the first firing entry alert as a webhook v4 body.

Selection: the Lite rule (``scripts.itbench_lite.alerts.select_entry_alert``:
baseline alert names dropped, ``state == firing`` with an ``activeAt``,
earliest ``activeAt``, ties by sorted labels) applied to the alerts whose
label set was not already pending or firing before the fault was injected.
The webhook is Lite's ``build_webhook`` with offset 0: ``startsAt`` is the
environment's own ``activeAt`` (A564-7).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from scripts.itbench_lite.alerts import (
    EntryTarget,
    NoEntryAlert,
    build_webhook,
    select_entry_alert,
)
from scripts.itbench_lite.alerts import _canonical_labels as canonical_labels

ALERTS_PATH = "/api/v1/alerts"


class AlertsUnreadable(ValueError):
    """Prometheus' answer is not a ``/api/v1/alerts`` success body."""


def parse_alerts(body: object) -> list[dict[str, Any]]:
    if not isinstance(body, Mapping) or body.get("status") != "success":
        raise AlertsUnreadable("not a success body")
    data = body.get("data")
    alerts = data.get("alerts") if isinstance(data, Mapping) else None
    if not isinstance(alerts, list):
        raise AlertsUnreadable("no data.alerts list")
    return [dict(a) for a in alerts if isinstance(a, Mapping)]


def label_key(alert: Mapping[str, Any]) -> str | None:
    labels = alert.get("labels")
    if not isinstance(labels, Mapping) or not labels:
        return None
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        return None
    return canonical_labels(labels)


def prefault_keys(alerts: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Label sets pending or firing before injection (environment noise)."""
    keys = set()
    for alert in alerts:
        if alert.get("state") in ("pending", "firing"):
            key = label_key(alert)
            if key is not None:
                keys.add(key)
    return frozenset(keys)


def new_alerts(
    alerts: Sequence[Mapping[str, Any]], excluded: frozenset[str]
) -> list[dict[str, Any]]:
    return [
        dict(a)
        for a in alerts
        if (key := label_key(a)) is not None and key not in excluded
    ]


def entry_alert(
    alerts: Sequence[Mapping[str, Any]], excluded: frozenset[str]
) -> dict[str, Any] | None:
    """The entry alert among the post-fault alerts, or ``None`` yet."""
    try:
        return select_entry_alert(new_alerts(alerts, excluded))
    except NoEntryAlert:
        return None


def webhook(
    alert: Mapping[str, Any], match_labels: Mapping[str, str]
) -> dict[str, Any]:
    return build_webhook(
        alert, offset_seconds=0, target=EntryTarget(match_labels=dict(match_labels))
    )


def summary_of(alert: Mapping[str, Any]) -> dict[str, Any]:
    """What the summary keeps of the entry alert: name, labels, activeAt.
    Annotations are untrusted text and stay out of the evidence."""
    labels = alert.get("labels") if isinstance(alert.get("labels"), Mapping) else {}
    return {
        "alertname": labels.get("alertname") if isinstance(labels, Mapping) else None,
        "labels": dict(labels) if isinstance(labels, Mapping) else {},
        "activeAt": alert.get("activeAt"),
    }


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
