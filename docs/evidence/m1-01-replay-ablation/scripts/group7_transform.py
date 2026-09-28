# Group 7: isolated-context Codex proposal (verbatim from codex-isolated-diagnosis.out)
import copy
import json

_STATUS_GUARD = (
    'Final-report status discipline: status_state="not_recorded" or '
    "status_tags={} means that this visible span has no status tag (OTel "
    "Unset). It is not status code 0, HTTP 200, or OK. Mention a code or OK "
    "only when that exact key/value is present on the same visible row. "
    'Missing metric series means unknown, not zero; say "no series was '
    'returned" rather than "no non-zero code exists."'
)

_FINAL_GUARD = (
    "Before emitting the final JSON, apply these checks: (1) use "
    "spans_shown for visible span rows and keep trace counts, backend spans, "
    "and omitted spans separate; (2) for every count or range, use only rows "
    "with identical service, operation, and metric labels and copy the "
    "visible-row aggregate in model_view_index; never combine parent/child "
    "operations or units, and omit the range if no aggregate exists; "
    "(3) count listed IDs literally and re-check every summary number; "
    "(4) a parent-child statement requires the child's parent_references "
    "entry with parent_is_visible=true; a missing/false parent is unknown "
    "and a grandchild is not a child; (5) every factual numeric or status "
    "clause must cite the evidence_id of the view containing it; split "
    "claims when sources differ; (6) preserve incomplete, truncated, and "
    "no_data views as gaps."
)


def _add_trace_index(payload):
    if "model_view_index" in payload:
        return False

    rows = payload.get("content")
    if not isinstance(rows, list):
        return False

    spans = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("operation"), str)
        and ("duration_us" in row)
        and ("status_state" in row or "status_tags" in row)
    ]
    if not spans:
        return False

    groups = {}
    for row in spans:
        key = (
            row.get("service") if isinstance(row.get("service"), str) else "",
            row["operation"],
        )
        g = groups.setdefault(
            key,
            {
                "visible_rows": 0,
                "error_by_visible_tags_true": 0,
                "status_state_counts": {},
                "status_tags_counts": {},
                "duration_us_min": None,
                "duration_us_max": None,
            },
        )
        g["visible_rows"] += 1
        if row.get("error_by_visible_tags") is True:
            g["error_by_visible_tags_true"] += 1

        state = row.get("status_state")
        if isinstance(state, str):
            g["status_state_counts"][state] = g["status_state_counts"].get(state, 0) + 1

        tags = row.get("status_tags")
        if not isinstance(tags, dict):
            tags = {}
        tag_key = json.dumps(
            tags, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        g["status_tags_counts"][tag_key] = g["status_tags_counts"].get(tag_key, 0) + 1

        duration = row.get("duration_us")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            g["duration_us_min"] = (
                duration
                if g["duration_us_min"] is None
                else min(g["duration_us_min"], duration)
            )
            g["duration_us_max"] = (
                duration
                if g["duration_us_max"] is None
                else max(g["duration_us_max"], duration)
            )

    edges = []
    for row in spans:
        for ref in row.get("parent_references") or []:
            if isinstance(ref, dict) and ref.get("parent_is_visible") is True:
                edges.append(
                    {
                        "trace_id": row.get("trace_id"),
                        "child_span_id": row.get("span_id"),
                        "parent_span_id": ref.get("spanID"),
                        "ref_type": ref.get("refType"),
                    }
                )
    edges.sort(
        key=lambda x: (
            str(x.get("trace_id")),
            str(x.get("parent_span_id")),
            str(x.get("child_span_id")),
        )
    )

    payload["model_view_index"] = {
        "derived_from": "visible content rows only; omitted rows remain unknown",
        "evidence_id": payload.get("evidence_id"),
        "trace_groups": [
            {
                "service": service,
                "operation": operation,
                **groups[(service, operation)],
            }
            for service, operation in sorted(groups)
        ],
        "visible_parent_edges": edges,
    }
    return True


def transform(request_body: dict) -> dict:
    out = copy.deepcopy(request_body)
    messages = out.get("messages")
    if not isinstance(messages, list):
        raise ValueError("request_body.messages must be a list")

    system = next((m for m in messages if m.get("role") == "system"), None)
    if not isinstance(system, dict) or not isinstance(system.get("content"), str):
        raise ValueError("system message missing")
    if _STATUS_GUARD not in system["content"]:
        system["content"] += "\n\n" + _STATUS_GUARD

    for message in messages:
        if message.get("role") != "tool":
            continue
        raw = message.get("content")
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and _add_trace_index(payload):
            message["content"] = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )

    for message in reversed(messages):
        if (
            message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith(
                "Run coverage summary, computed from the structured fields"
            )
        ):
            if _FINAL_GUARD not in message["content"]:
                message["content"] += "\n\n" + _FINAL_GUARD
            break

    return out
