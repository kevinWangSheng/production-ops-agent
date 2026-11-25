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
    BASELINE_ALERTNAMES,
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


# -- diagnostics (#866) ----------------------------------------------------------------------
#
# Environment alerts, the same input the product receives; never answers. The
# details go to the trial's git-ignored ``trial.json``; the summary keeps the
# counts of ``AlertTimeline.counts`` only.

QUERY_RANGE_PATH = "/api/v1/query_range"
#: Prometheus' own series of every pending or firing alert.
ALERTS_SERIES = "ALERTS"


def observation(alert: Mapping[str, Any]) -> dict[str, Any] | None:
    key = label_key(alert)
    if key is None:
        return None
    labels = alert.get("labels")
    assert isinstance(labels, Mapping)
    return {
        "labels": dict(labels),
        "state": alert.get("state"),
        "activeAt": alert.get("activeAt"),
    }


class AlertTimeline:
    """Every distinct ``(label set, state, activeAt)`` seen at the pre-fault
    snapshot (poll 0) and at each poll, with the first and last poll that
    saw it."""

    def __init__(self) -> None:
        self.prefault: dict[str, dict[str, Any]] = {}
        self.seen: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.polls: list[dict[str, Any]] = []

    def record_prefault(
        self, alerts: Sequence[Mapping[str, Any]], at: str | None
    ) -> None:
        for alert in alerts:
            item = observation(alert)
            if item is not None and item["state"] in ("pending", "firing"):
                self.prefault[label_key(alert) or ""] = {**item, "at": at}

    def record_poll(
        self, poll: int, at: str | None, alerts: Sequence[Mapping[str, Any]]
    ) -> None:
        self.polls.append({"poll": poll, "at": at, "alerts": len(alerts)})
        for alert in alerts:
            item = observation(alert)
            if item is None:
                continue
            key = (label_key(alert) or "", str(item["state"]), str(item["activeAt"]))
            entry = self.seen.get(key)
            if entry is None:
                self.seen[key] = {**item, "first_poll": poll, "first_at": at,
                                  "last_poll": poll, "last_at": at}  # fmt: skip
            else:
                entry.update(last_poll=poll, last_at=at)

    def counts(self) -> dict[str, int]:
        """Sanitised codes for the summary (no labels)."""
        pre = self.prefault
        new_keys = {k for k, _, _ in self.seen if k not in pre}
        firing_new = {k for k, s, _ in self.seen if k not in pre and s == "firing"}
        baseline_new = {
            k for k, s, _ in self.seen
            if k in firing_new and self._name(k) in BASELINE_ALERTNAMES
        }  # fmt: skip
        turned = {
            k for k, s, _ in self.seen
            if k in pre and pre[k]["state"] == "pending" and s == "firing"
        }  # fmt: skip
        refired = {
            k for k, s, a in self.seen
            if k in pre and a != str(pre[k]["activeAt"])
        }  # fmt: skip
        still = {k for k, _, _ in self.seen if k in pre}
        return {
            "prefault_sets": len(pre),
            "prefault_pending_sets": sum(
                1 for v in pre.values() if v["state"] == "pending"
            ),
            "prefault_pending_turned_firing": len(turned),
            "prefault_sets_reactivated": len(refired),
            "prefault_sets_gone": len(set(pre) - still),
            "new_sets_seen": len(new_keys),
            "new_sets_firing": len(firing_new),
            "new_sets_firing_baseline_name": len(baseline_new),
            "new_sets_pending_only": len(new_keys - firing_new),
            "polls": len(self.polls),
        }

    def _name(self, key: str) -> str | None:
        for item in (*self.prefault.values(), *self.seen.values()):
            if canonical_labels(item["labels"]) == key:
                name = item["labels"].get("alertname")
                return name if isinstance(name, str) else None
        return None

    def details(self) -> dict[str, Any]:
        return {
            "prefault": list(self.prefault.values()),
            "observations": sorted(
                self.seen.values(), key=lambda v: (v["first_poll"], dumps(v["labels"]))
            ),
            "polls": self.polls,
        }


def range_series(body: object) -> list[dict[str, Any]]:
    """``ALERTS`` range result -> per-series labels with first and last sample
    time (unix seconds) and sample count; an unreadable body is no series."""
    data = body.get("data") if isinstance(body, Mapping) else None
    result = data.get("result") if isinstance(data, Mapping) else None
    out: list[dict[str, Any]] = []
    for series in result if isinstance(result, list) else []:
        if not isinstance(series, Mapping):
            continue
        metric = series.get("metric")
        values = series.get("values")
        if (
            not isinstance(metric, Mapping)
            or not isinstance(values, list)
            or not values
        ):
            continue
        stamps = [float(v[0]) for v in values if isinstance(v, list) and v]
        if not stamps:
            continue
        out.append({
            "labels": {str(k): str(v) for k, v in metric.items() if k != "__name__"},
            "first": min(stamps), "last": max(stamps), "samples": len(stamps),
        })  # fmt: skip
    return out


def range_counts(
    series: Sequence[Mapping[str, Any]], prefault: Mapping[str, Any], inject_ts: float
) -> dict[str, int]:
    """Firing series (``alertstate="firing"``) whose label set, without
    ``alertstate``, is new or first fires after injection."""
    firing = [s for s in series if s["labels"].get("alertstate") == "firing"]
    after = new = 0
    for s in firing:
        labels = {k: v for k, v in s["labels"].items() if k != "alertstate"}
        key = canonical_labels(labels)
        if s["first"] >= inject_ts:
            after += 1
            if key not in prefault:
                new += 1
    return {
        "range_firing_series": len(firing),
        "range_firing_first_after_inject": after,
        "range_firing_new_after_inject": new,
    }
