"""Group 3 context-shape transform: add ``span_groups`` to traces_search views.

Pure function over the view JSON already in the tool-result message. Rows are
grouped by ``(service, operation)`` in first-appearance order; every number
is computed from the rows shown, the rows themselves are untouched, and no
instruction text changes. ``status`` counts rows by their recorded status
tags (``key=value`` pairs joined by ``;`` in key order) or ``not_recorded``
when ``status_state`` is ``not_recorded`` or ``status_tags`` is empty.
"""

from __future__ import annotations

import json
from typing import Any

from opspilot.tools.registry import canonical


def status_key(row: dict[str, Any]) -> str:
    tags = row.get("status_tags")
    if (
        row.get("status_state") == "not_recorded"
        or not isinstance(tags, dict)
        or not tags
    ):
        return "not_recorded"
    return ";".join(
        f"{k}={json.dumps(tags[k])}" if isinstance(tags[k], str) else f"{k}={tags[k]}"
        for k in sorted(tags)
    )


def span_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("service")), str(row.get("operation")))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "service": key[0],
                "operation": key[1],
                "rows": 0,
                "status": {},
                "error_rows": 0,
                "duration_us_min": None,
                "duration_us_max": None,
            }
        group["rows"] += 1
        status = status_key(row)
        group["status"][status] = group["status"].get(status, 0) + 1
        if row.get("error_by_visible_tags") is True:
            group["error_rows"] += 1
        duration = row.get("duration_us")
        if isinstance(duration, int):
            lo, hi = group["duration_us_min"], group["duration_us_max"]
            group["duration_us_min"] = duration if lo is None else min(lo, duration)
            group["duration_us_max"] = duration if hi is None else max(hi, duration)
    return list(groups.values())


def add_span_groups(view: dict[str, Any]) -> dict[str, Any]:
    if view.get("tool") != "traces_search" or not isinstance(view.get("content"), list):
        return view
    rows = [r for r in view["content"] if isinstance(r, dict)]
    return {**view, "span_groups": span_groups(rows)}


def transform_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for message in messages:
        if message.get("role") == "tool":
            view = json.loads(message["content"])
            if isinstance(view, dict) and view.get("tool") == "traces_search":
                message = {**message, "content": canonical(add_span_groups(view))}
        out.append(message)
    return out


if __name__ == "__main__":
    import sys

    messages = json.load(open(sys.argv[1]))
    for message in transform_messages(messages):
        if message.get("role") == "tool":
            view = json.loads(message["content"])
            if "span_groups" in view:
                print(view["evidence_id"], json.dumps(view["span_groups"], indent=1))
