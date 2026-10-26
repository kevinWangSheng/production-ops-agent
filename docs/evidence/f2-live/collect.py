"""Freeze one F2 PR-E kind scenario into ``<dir>/summary.json`` (not product code).

    collect.py k1|k2|k3|k4|k5 --out <summary.json> [scenario ids...]

Reads product rows with the owner DSN in a READ ONLY snapshot, the ledger
files that ``ctl.py`` and the harness wrote (only their sha256 and the fields
named below), and LangSmith root runs by ``metadata.run_id``. Verdicts are
deterministic predicates over those rows; nothing here asks a model.
Environment as ``ctl.py`` (F2_LIVE_DSN, F2_LIVE_LEDGER, F2_LIVE_SECRETS) plus
F2_LIVE_LAB_LEDGERS (shared harness ledgers) and F2_LIVE_PROJECT.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path.cwd()))

from opspilot.acceptance import (  # noqa: E402
    IncidentScenario,
    alert_intake_outcome,
    alert_intake_records,
    outcome_from_durable,
)
from opspilot.persistence import DurableStore  # noqa: E402
from opspilot.web.store import DurableIncidentStore  # noqa: E402

DSN = os.environ["F2_LIVE_DSN"]
LEDGER = Path(os.environ["F2_LIVE_LEDGER"])
LAB_LEDGERS = Path(os.environ.get("F2_LIVE_LAB_LEDGERS", LEDGER.parent))
PROJECT = os.environ.get("F2_LIVE_PROJECT", "opspilot-lab-f2-live-20261024")
EVENT_KEYS = {
    "channel",
    "text",
    "fingerprint",
    "starts_at",
    "status",
    "annotation_revision",
    "delivery_key",
}
TIMEOUT_TEXT = "source may have received the request"


def iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, dict):
        return {k: iso(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [iso(x) for x in v]
    return v


def q(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [iso(dict(r)) for r in conn.execute(sql, params).fetchall()]


def sha(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "file": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def ledger_json(label: str) -> dict[str, Any]:
    return json.loads((LEDGER / f"{label}.json").read_text())


def check(results: list[dict[str, Any]], name: str, ok: bool, actual: Any) -> None:
    results.append({"assertion": name, "pass": bool(ok), "actual": actual})


# -- shared reads ---------------------------------------------------------------


def runs_of(conn: Any, incident: str) -> list[dict[str, Any]]:
    return q(
        conn,
        "SELECT run_id,state,epoch,control_generation,budget_limit,budget_spent,budget_reserved,"
        "tool_operations_used,deadline,versions,input_watermark FROM opspilot_runs "
        "WHERE incident_id=%s ORDER BY control_generation,run_id",
        (incident,),
    )


def incident_row(conn: Any, incident: str) -> dict[str, Any]:
    return q(
        conn,
        "SELECT incident_id,created_at,lifecycle,state,mode,control_generation,current_run_id,"
        "priority,related_incident_id,conclusion IS NOT NULL AS concluded FROM opspilot_incidents "
        "WHERE incident_id=%s",
        (incident,),
    )[0]


def events_of(conn: Any, subject: str) -> list[dict[str, Any]]:
    return q(
        conn,
        "SELECT sequence,kind,payload,recorded_at FROM opspilot_subject_events "
        "WHERE subject_id::text=%s ORDER BY sequence",
        (subject,),
    )


def steps_of(conn: Any, run: str) -> list[dict[str, Any]]:
    return q(
        conn,
        "SELECT step_id,sequence,logical_key,status,control_generation,observed_at,"
        "jsonb_array_length(tool_results) AS tool_results FROM opspilot_steps "
        "WHERE run_id=%s ORDER BY sequence",
        (run,),
    )


def tool_rows(conn: Any, run: str) -> list[dict[str, Any]]:
    return q(
        conn,
        "SELECT s.sequence,s.step_id,(r->>'ordinal')::int AS ordinal,r->'result'->>'tool' AS tool,"
        "r->'result'->>'status' AS status,r->'result'->>'reason' AS reason,"
        "r->'result'->>'source_contact' AS source_contact,r->'result'->>'adopted' AS adopted,"
        "r->'result'->>'operation_id' AS operation_id,r->'result'->>'requested_at' AS requested_at,"
        "r->'result'->'query' AS query,r->'result'->'window' AS window "
        "FROM opspilot_steps s, jsonb_array_elements(s.tool_results) r "
        "WHERE s.run_id=%s ORDER BY s.sequence,ordinal",
        (run,),
    )


def durable_projection(step: str, sid: str, incident: str) -> dict[str, Any]:
    store = DurableStore(DSN)
    try:
        snapshot = store.rebuild(UUID(incident))
        controls = tuple(
            asdict(r) for r in DurableIncidentStore(store).control_audit(UUID(incident))
        )
        with psycopg.connect(DSN, row_factory=dict_row) as conn:
            handoffs = tuple(
                e["payload"]
                for e in events_of(conn, incident)
                if e["kind"] == "run_handoff"
            )
        outcome = outcome_from_durable(
            IncidentScenario(f"f2-live-{sid}", "F2", step, "incident", incident),
            snapshot,
            controls=controls,
            handoff_events=handoffs,
        )
    finally:
        store.close()
    return iso(asdict(outcome))


def intake_projection(step: str, sid: str, incident: str) -> dict[str, Any]:
    with psycopg.connect(DSN) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        records = alert_intake_records(conn, UUID(incident))
    outcome = alert_intake_outcome(
        IncidentScenario(f"f2-live-{sid}", "F2", step, "alert-intake", incident),
        records,
    )
    data = iso(asdict(outcome))
    for d in data.get("deliveries", []) or []:
        if isinstance(d, dict):
            d.pop("delivery_key", None)
    return data


def traces(run_id: str) -> list[dict[str, Any]]:
    """Root runs of every attempt of ``run_id`` with llm/tool child counts."""
    from scripts.f2_kill_resume import read_env_file
    from scripts.lab_evidence import langsmith_client, run_url

    secrets = read_env_file(Path(os.environ["F2_LIVE_SECRETS"]))
    client = langsmith_client(
        {
            "OPSPILOT_TRACE": "lab",
            "LANGSMITH_API_KEY": secrets.get("LANGSMITH_API_KEY", ""),
        }
    )
    roots = list(
        client.list_runs(
            project_name=PROJECT,
            is_root=True,
            filter=f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))',
        )
    )
    found = []
    for root in sorted(roots, key=lambda r: r.start_time):
        kids = list(client.list_runs(project_name=PROJECT, trace_id=root.trace_id))
        found.append(
            {
                "langsmith_run_id": str(root.id),
                "otel_trace_id": (root.extra or {})
                .get("metadata", {})
                .get("OTEL_TRACE_ID"),
                "start_time": iso(root.start_time),
                "status": root.status,
                "url": run_url(
                    client,
                    project_id=str(root.session_id),
                    langsmith_run_id=str(root.id),
                ),
                "llm": [k for k in kids if k.run_type == "llm"],
                "tool": [k for k in kids if k.run_type == "tool"],
            }
        )
    return found


def trace_summary(
    found: list[dict[str, Any]], needle: str | None = None
) -> list[dict[str, Any]]:
    out = []
    for t in found:
        row = {k: v for k, v in t.items() if k not in ("llm", "tool")}
        row["llm_spans"] = len(t["llm"])
        row["tool_spans"] = len(t["tool"])
        row["input_tokens"] = sum((k.prompt_tokens or 0) for k in t["llm"])
        row["output_tokens"] = sum((k.completion_tokens or 0) for k in t["llm"])
        if needle is not None:
            row["llm_inputs_with_text"] = sum(
                1 for k in t["llm"] if needle in json.dumps(k.inputs, default=str)
            )
        out.append(row)
    return out


def code_versions(runs: list[dict[str, Any]]) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    return {
        "commit": head,
        "run_versions": sorted(
            {json.dumps(r["versions"], sort_keys=True) for r in runs}
        ),
    }


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# -- scenarios ------------------------------------------------------------------


def k1(conn: Any, a: argparse.Namespace) -> dict[str, Any]:
    inc = a.ids[0]
    res: list[dict[str, Any]] = []
    deliveries = q(
        conn,
        "SELECT received_at,outcome,status,starts_at,fingerprint,annotation_revision,actor "
        "FROM opspilot_alert_deliveries WHERE incident_id=%s ORDER BY received_at",
        (inc,),
    )
    all_deliveries = q(conn, "SELECT count(*) AS n FROM opspilot_alert_deliveries")[0][
        "n"
    ]
    runs = runs_of(conn, inc)
    events = events_of(conn, inc)
    ev_inputs = q(
        conn,
        "SELECT input_id,sequence,kind,content,actor,control_generation,received_at FROM opspilot_inputs "
        "WHERE incident_id=%s AND kind='event' ORDER BY sequence",
        (inc,),
    )
    identities = q(
        conn,
        "SELECT starts_at,incident_id,run_id FROM opspilot_alert_identities ORDER BY created_at",
    )
    outcomes = [(d["status"], d["outcome"]) for d in deliveries]
    check(
        res,
        "deliveries: firing created -> resolved resolved_attached -> new startsAt firing attached -> resolved resolved_attached",
        outcomes
        == [
            ("firing", "created"),
            ("resolved", "resolved_attached"),
            ("firing", "attached"),
            ("resolved", "resolved_attached"),
        ]
        and deliveries[2]["starts_at"] != deliveries[0]["starts_at"]
        and all_deliveries == 4,
        {"deliveries": deliveries, "all_deliveries_in_db": all_deliveries},
    )
    attach_at = utc(deliveries[2]["received_at"])
    follow = ledger_json("k1-follow-up")
    follow_at = utc(follow["at"])
    created_between = [
        e
        for e in events
        if attach_at <= utc(e["recorded_at"]) < follow_at and e["kind"] not in ()
    ]
    runs_before_follow_up = follow["before"]["runs"]
    check(
        res,
        "attach: same incident, no new Run, no announce between attach and follow_up, one event input with whitelisted keys, no identity row",
        len(runs_before_follow_up) == 1
        and not [
            e
            for e in created_between
            if e["kind"] in ("run_created", "intake_accepted", "run_claimed")
        ]
        and len(ev_inputs) == 1
        and set(ev_inputs[0]["content"]) <= EVENT_KEYS
        and len(identities) == 1,
        {
            "runs_before_follow_up": runs_before_follow_up,
            "subject_events_between_attach_and_follow_up": [
                e["kind"] for e in created_between
            ],
            "event_input": {k: v for k, v in ev_inputs[0].items() if k != "content"}
            | {
                "content": {
                    k: v
                    for k, v in ev_inputs[0]["content"].items()
                    if k != "delivery_key"
                }
            },
            "event_content_keys": sorted(ev_inputs[0]["content"]),
            "identity_rows": len(identities),
        },
    )
    event_text = ev_inputs[0]["content"]["text"]
    succ = follow["http_body"]["run_id"]
    first = runs[0]["run_id"]
    succ_run = next(r for r in runs if r["run_id"] == succ)
    succ_events = [e for e in events if e["kind"] == "run_handoff"]
    found = traces(succ)
    tsum = trace_summary(found, event_text)
    check(
        res,
        "C4-A assertion 2: follow_up after the attachment runs a Run with model calls, no INCOMPATIBLE_STATE, trace inputs carry the event text",
        follow["http_status"] == 200
        and succ != first
        and succ_run["budget_spent"] > 0
        and succ_run["state"] == "completed"
        and not any(
            "INCOMPATIBLE_STATE" in json.dumps(e["payload"]) for e in succ_events
        )
        and tsum
        and tsum[-1]["llm_inputs_with_text"] == tsum[-1]["llm_spans"] > 0,
        {
            "follow_up": {k: follow[k] for k in ("at", "http_status", "http_body")},
            "branch": "first Run concluded -> follow_up opened a successor Run (E-R4 branch A1)",
            "successor_run": succ_run,
            "handoff_events": succ_events,
            "trace": tsum,
        },
    )
    inputs = {
        r["run_id"]: q(
            conn,
            "SELECT input->'evidence_context'->'time_policies'->0 AS tp FROM opspilot_runs WHERE run_id=%s",
            (r["run_id"],),
        )[0]["tp"]
        for r in runs
    }
    tp = inputs[succ]
    created = utc(succ_run["deadline"]) - timedelta(seconds=7200)
    w_end, w_start = utc(tp["window"]["end"]), utc(tp["window"]["start"])
    anchor = utc(tp["anchor"])
    episode = utc(deliveries[2]["starts_at"])
    check(
        res,
        "#305 W1-B: successor frame ends at its creation DB instant (UTC second), 24 h long, anchor = attached episode startsAt, default window [anchor-1h, frame end]",
        w_end == created.replace(microsecond=0)
        and w_end - w_start == timedelta(hours=24)
        and anchor == episode
        and utc(tp["default_query_window"]["start"]) == anchor - timedelta(hours=1)
        and utc(tp["default_query_window"]["end"]) == w_end,
        {
            "first_run_time_policy": inputs[first],
            "successor_time_policy": tp,
            "successor_created_at_from_deadline": created.isoformat(),
        },
    )
    covering = [
        t
        for t in tool_rows(conn, succ)
        if t["tool"] == "metrics_range_query"
        and t["status"] == "ok"
        and t["query"]
        and utc(t["query"]["start"]) <= episode <= utc(t["query"]["end"])
    ]
    covering_ops = {f"{c['step_id']}-t{c['ordinal']}" for c in covering}
    trace_ok = []
    for t in found[-1]["tool"] if found else []:
        evidence_id = str(
            (t.extra or {})
            .get("metadata", {})
            .get("otel.span.opspilot.tool.evidence_id", "")
        )
        if evidence_id.split(":", 1)[0] in covering_ops:
            outputs = t.outputs or {}
            trace_ok.append(
                {
                    "tool_span": str(t.id),
                    "operation_id": evidence_id.split(":", 1)[0],
                    "output_status_ok": outputs.get("status") == "ok"
                    or (
                        outputs.get("adopted") is True and bool(outputs.get("content"))
                    ),
                }
            )
    check(
        res,
        "#305: at least one metric query covering the attached episode returned ok (rows) and its tool span is in the trace",
        len(covering) >= 1 and any(x["output_status_ok"] for x in trace_ok),
        {
            "episode_starts_at": episode.isoformat(),
            "covering_ok_queries": len(covering),
            "first": {
                k: covering[0][k]
                for k in ("sequence", "ordinal", "status", "adopted", "query")
            }
            if covering
            else None,
            "trace_tool_spans": trace_ok[:3],
        },
    )
    page = (LEDGER / "k1-page-after-attach.html").read_text()
    check(
        res,
        "page: re-fired episode visible on the incident page",
        "re-fired episode" in page,
        {"page_sha256": hashlib.sha256(page.encode()).hexdigest()},
    )
    return {
        "scenario": "K1 re-firing -> attached -> human follow_up (+ #305)",
        "incident_id": inc,
        "runs": runs,
        "first_run_trace": trace_summary(traces(first)),
        "assertions": res,
        "intake_projection": intake_projection("1", "k1", inc),
        "ledger": [
            sha(LEDGER / f)
            for f in (
                "k1-follow-up.json",
                "k1-page-after-attach.html",
                "k1-page-end.html",
                "k1-close.json",
            )
        ],
        "code": code_versions(runs),
    }


def k2(conn: Any, a: argparse.Namespace) -> dict[str, Any]:
    intake = ledger_json("k2-intake")
    inc, run = intake["http_body"]["incident_id"], intake["http_body"]["run_id"]
    res: list[dict[str, Any]] = []
    tools = tool_rows(conn, run)
    proxy = [
        json.loads(line)
        for line in (LEDGER / "proxy-k2.log").read_text().splitlines()
        if line.startswith("{")
    ]
    stalled_proxy = [p for p in proxy if p["stalled"]]
    timeouts = [t for t in tools if t["status"] == "timeout"]
    t0 = timeouts[0] if timeouts else None
    charges = q(
        conn,
        "SELECT operation_id,epoch,seconds,reserved FROM opspilot_tool_charges WHERE run_id=%s",
        (run,),
    )
    charge = [c for c in charges if t0 and c["operation_id"] == t0["operation_id"]]
    req = utc(t0["requested_at"]) if t0 else None
    disc = utc(stalled_proxy[0]["client_disconnected_at"]) if stalled_proxy else None
    check(
        res,
        "stalled query_range ends timeout / TOOL_TIMEOUT / possible within 30 s (+1 s), not adopted",
        len(stalled_proxy) == 1
        and len(timeouts) == 1
        and t0["tool"] == "metrics_range_query"
        and t0["reason"] == "TOOL_TIMEOUT"
        and t0["source_contact"] == "possible"
        and t0["adopted"] in (None, "false")
        and len(charge) == 1
        and charge[0]["seconds"] <= 31
        and (disc - req).total_seconds() <= 31,
        {
            "tool_result": {
                k: t0[k]
                for k in (
                    "sequence",
                    "ordinal",
                    "operation_id",
                    "status",
                    "reason",
                    "source_contact",
                    "adopted",
                    "requested_at",
                )
            }
            if t0
            else None,
            "gateway_seconds": charge[0]["seconds"] if charge else None,
            "proxy_stalled_record": stalled_proxy,
            "request_to_client_close_seconds": (disc - req).total_seconds()
            if t0 and disc
            else None,
        },
    )
    siblings = [
        t
        for t in tools
        if t0 and t["step_id"] == t0["step_id"] and t["ordinal"] != t0["ordinal"]
    ]
    sib_ev = q(
        conn,
        "SELECT evidence_id,committed,adopted FROM opspilot_evidence WHERE run_id=%s AND evidence_id LIKE %s",
        (run, f"{t0['step_id']}-t%" if t0 else "none"),
    )
    check(
        res,
        "same-round sibling(s) ran, committed and kept",
        len(siblings) >= 1
        and all(s["status"] == "ok" for s in siblings)
        and all(e["committed"] for e in sib_ev)
        and len(sib_ev) >= 1,
        {
            "siblings": [
                {
                    k: s[k]
                    for k in ("ordinal", "tool", "status", "adopted", "operation_id")
                }
                for s in siblings
            ],
            "sibling_evidence": sib_ev,
        },
    )
    metric_calls = [t for t in tools if t["tool"] == "metrics_range_query"]
    run_end = max(utc(s["observed_at"]) for s in steps_of(conn, run))
    proxy_ranges = [
        p
        for p in proxy
        if p["path"] == "/api/v1/query_range"
        and utc(intake["at"]) <= utc(p["time"]) <= run_end
    ]
    check(
        res,
        "no gateway retry: the stalled operation dispatched once; every query_range at the proxy is one metrics_range_query in the Run",
        len(charge) == 1 and len(proxy_ranges) == len(metric_calls),
        {
            "proxy_query_range_during_run": len(proxy_ranges),
            "run_metrics_range_query": len(metric_calls),
            "charges_for_stalled_operation": len(charge),
        },
    )
    runs = runs_of(conn, inc)
    handoffs = [e for e in events_of(conn, inc) if e["kind"] == "run_handoff"]
    check(
        res,
        "Run ends completed (or an explicit handoff with reason), never silently hangs",
        runs[0]["state"] == "completed" or bool(handoffs),
        {"run": runs[0], "handoff_events": handoffs},
    )
    samples = [
        json.loads(line)
        for line in (LEDGER / "k2-threads.jsonl").read_text().splitlines()
    ]
    intake_at = utc(intake["at"])
    before = [s["threads"] for s in samples if utc(s["at"]) < intake_at]
    stall = [
        s["threads"] for s in samples if req and disc and req <= utc(s["at"]) < disc
    ]
    after5 = [
        s
        for s in samples
        if disc
        and disc + timedelta(seconds=4) <= utc(s["at"]) <= disc + timedelta(seconds=6)
    ]
    after_run = (
        [s["threads"] for s in samples if utc(s["at"]) > disc + timedelta(minutes=3)]
        if disc
        else []
    )
    pre_stall = [
        s["threads"]
        for s in samples
        if req
        and req - timedelta(seconds=6) <= utc(s["at"]) < req - timedelta(seconds=1)
    ]
    check(
        res,
        "worker threads (ps -M) return to the in-Run level 5 s after the timeout and to the pre-Run baseline after the Run",
        bool(before and after5 and after_run)
        and after5[0]["threads"] <= max(pre_stall or before)
        and max(after_run) <= max(before),
        {
            "pre_run_baseline": sorted(set(before)),
            "in_run_before_stall": pre_stall,
            "during_stall": sorted(set(stall)),
            "five_seconds_after_timeout": after5[:1],
            "after_run": sorted(set(after_run)),
            "measure": "ps -M -p <worker pid> line count minus header; 1 s samples",
        },
    )
    page = (LEDGER / "k2-page.html").read_text()
    check(
        res,
        "workbench tool row says the source may have received the request",
        page.count(TIMEOUT_TEXT) == 1,
        {"occurrences": page.count(TIMEOUT_TEXT)},
    )
    report = q(
        conn, "SELECT conclusion FROM opspilot_incidents WHERE incident_id=%s", (inc,)
    )[0]["conclusion"]
    gaps_text = json.dumps(report or {})
    return {
        "scenario": "K2 one stalled query with a same-round sibling (current main, parallel 2)",
        "incident_id": inc,
        "run_id": run,
        "assertions": res,
        "report_gaps_mention_timed_out_operation": bool(t0)
        and t0["operation_id"] in gaps_text,
        "trace": trace_summary(traces(run)),
        "projection": durable_projection("3", "k2", inc),
        "ledger": [
            sha(LEDGER / f)
            for f in (
                "k2-intake.json",
                "proxy-k2.log",
                "k2-threads.jsonl",
                "k2-page.html",
            )
        ],
        "code": code_versions(runs),
    }


def inflight_control(
    conn: Any, a: argparse.Namespace, key: str, action: str
) -> dict[str, Any]:
    intake = ledger_json(f"{key}-intake")
    inc, run = intake["http_body"]["incident_id"], intake["http_body"]["run_id"]
    ctl = ledger_json(f"{key}-{action}")
    res: list[dict[str, Any]] = []
    steps = steps_of(conn, run)
    after = ctl["after"]
    check(
        res,
        f"model request in flight (one unsettled reservation >= 1 s) when {action} was posted",
        ctl["located"]["held_seconds"] >= 1.0 and ctl["http_status"] == 200,
        {
            "located": {
                k: ctl["located"][k] for k in ("reservation_id", "held_seconds")
            },
            "posted_at": ctl["at"],
            "http_body": ctl["http_body"],
        },
    )
    late = [s for s in steps if s["status"] == "late_result"]
    first_late = late[0] if late else None
    want = "paused" if action == "pause" else "cancelled"
    check(
        res,
        f"Run {want}; the in-flight reply came back as late_result at the older control generation, no step advanced before {action}",
        after["incident"][0]["state"] == want
        and after["runs"][0]["state"] == want
        and first_late is not None
        and first_late["control_generation"]
        < after["incident"][0]["control_generation"]
        and steps[0]["status"] == "late_result",
        {
            "after_control": after["incident"][0],
            "run_after_control": after["runs"][0],
            "late_result_steps": late,
            "first_step": steps[0],
        },
    )
    runs = runs_of(conn, inc)
    events = events_of(conn, inc)
    out: dict[str, Any] = {"incident_id": inc, "run_id": run, "assertions": res}
    if action == "pause":
        resume = ledger_json(f"{key}-resume")
        check(
            res,
            "no conclusion while paused; resume requeues the same Run, which continues (epoch +1) and publishes",
            not ctl["after"]["incident"][0]["concluded"]
            and resume["http_status"] == 200
            and len(runs) == 1
            and runs[0]["run_id"] == run
            and runs[0]["epoch"] == 2
            and runs[0]["state"] == "completed",
            {
                "resume": {k: resume[k] for k in ("at", "http_status", "http_body")},
                "runs": runs,
            },
        )
        out["trace"] = trace_summary(traces(run))
    else:
        close = ledger_json(f"{key}-close")
        check(
            res,
            "cancel is final: Run and incident cancelled, no conclusion, no new Run (until the closing control)",
            not ctl["after"]["incident"][0]["concluded"]
            and len(close["before"]["runs"]) == 1
            and close["before"]["runs"][0]["state"] == "cancelled"
            and close["before"]["incident"][0]["state"] == "cancelled"
            and not [
                e for e in events if e["kind"] == "run_claimed" and e["sequence"] > 3
            ],
            {
                "before_close": close["before"],
                "subject_events": [(e["sequence"], e["kind"]) for e in events],
            },
        )
    out["projection"] = durable_projection("5", key, inc)
    proj = out["projection"]
    check(
        res,
        "acceptance projection (taken after the incidents were closed): late_result_rejected with STALE_CONTROL_GENERATION",
        "late_result_rejected" in proj["actions"]
        and "STALE_CONTROL_GENERATION" in proj["handoff_reasons"],
        {
            k: proj[k]
            for k in ("final_state", "human_interaction", "actions", "handoff_reasons")
        }
        | {
            "note": "projected after the session's closing control, so human_interaction reflects the incident's last control state, not the pause/cancel"
        },
    )
    out["reservations"] = q(
        conn,
        "SELECT reservation_id,state,seconds FROM opspilot_budget_reservations WHERE run_id=%s ORDER BY reservation_id",
        (run,),
    )
    out["runs"] = runs
    out["ledger"] = [
        sha(LEDGER / f) for f in sorted(os.listdir(LEDGER)) if f.startswith(key + "-")
    ]
    out["code"] = code_versions(runs)
    return out


def k3(conn: Any, a: argparse.Namespace) -> dict[str, Any]:
    return {
        "scenario": "K3 incident-level pause while a model request is in flight"
    } | inflight_control(conn, a, "k3", "pause")


def k4(conn: Any, a: argparse.Namespace) -> dict[str, Any]:
    return {
        "scenario": "K4 incident-level cancel while a model request is in flight"
    } | inflight_control(conn, a, "k4", "cancel")


def k5(conn: Any, a: argparse.Namespace) -> dict[str, Any]:
    run_dir = LAB_LEDGERS / a.ids[0]
    observation = a.ids[1]
    res: list[dict[str, Any]] = []
    obs = q(
        conn,
        "SELECT observation_id,source_release_id,status,ended_reason,ended_at,released_at,incident_id,used_sample_count,calibrated_policy_revision FROM opspilot_release_observations WHERE observation_id=%s",
        (observation,),
    )[0]
    links = q(
        conn,
        "SELECT disposition,incident_id,related_incident_id,run_id,input_id,consumed_at FROM opspilot_release_incident_links WHERE observation_id=%s",
        (observation,),
    )
    released = utc(obs["released_at"])
    new_incidents = q(
        conn,
        "SELECT incident_id,created_at,priority,related_incident_id FROM opspilot_incidents WHERE created_at>=%s ORDER BY created_at",
        (released,),
    )
    deliveries = q(
        conn,
        "SELECT received_at,outcome,status,starts_at,fingerprint,incident_id FROM opspilot_alert_deliveries WHERE received_at>=%s ORDER BY received_at",
        (released,),
    )
    link = links[0] if links else {}
    inc = link.get("incident_id")
    firings = [d for d in deliveries if d["status"] == "firing"]
    first_fire = firings[0] if firings else None
    order = (
        "release_first"
        if link.get("consumed_at")
        and first_fire
        and utc(link["consumed_at"]) < utc(first_fire["received_at"])
        else "alert_first_or_absent"
    )
    check(
        res,
        "release anomaly consumed first: link created, one new incident + Run",
        link.get("disposition") == "created" and order == "release_first",
        {"observation": obs, "links": links, "order": order},
    )
    runs = runs_of(conn, inc) if inc else []
    all_new_runs = q(
        conn,
        "SELECT r.run_id,r.incident_id,r.state FROM opspilot_runs r JOIN opspilot_incidents i USING(incident_id) WHERE i.created_at>=%s",
        (released,),
    )
    check(
        res,
        "alert first delivery attaches to the release incident: same incident, no new Run, one Run for the fault in the whole DB",
        first_fire is not None
        and first_fire["outcome"] == "attached"
        and first_fire["incident_id"] == inc
        and len(runs) == 1
        and len(all_new_runs) == 1
        and len(new_incidents) == 1,
        {
            "deliveries_after_release": deliveries,
            "release_incident_runs": runs,
            "runs_in_db_since_release": all_new_runs,
            "incidents_since_release": new_incidents,
        },
    )
    ev = (
        q(
            conn,
            "SELECT sequence,actor,content,received_at FROM opspilot_inputs WHERE incident_id=%s AND kind='event' ORDER BY sequence",
            (inc,),
        )
        if inc
        else []
    )
    alert_ev = [e for e in ev if e["actor"] == "alertmanager"]
    for e in ev:
        e["content"] = {k: v for k, v in e["content"].items() if k != "delivery_key"}
    inc_row = incident_row(conn, inc) if inc else {}
    check(
        res,
        "one alert event on the release incident with the release-anomaly template; control generation unchanged",
        len(alert_ev) == 1
        and "attached to this release-anomaly incident"
        in alert_ev[0]["content"]["text"]
        and inc_row.get("control_generation") == 0,
        {"event_inputs": ev, "incident": inc_row},
    )
    tsum = []
    if runs:
        tsum = trace_summary(
            traces(runs[0]["run_id"]),
            alert_ev[0]["content"]["text"] if alert_ev else None,
        )
    return {
        "scenario": "K5 #343: release anomaly opens first, the alert attaches (default 30 s pass, order not controlled)",
        "release_run": a.ids[0],
        "incident_id": inc,
        "assertions": res,
        "release_run_trace": tsum,
        "intake_projection": intake_projection("1", "k5", inc) if inc else None,
        "harness_ledger": [sha(p) for p in sorted(run_dir.glob("*.json*"))],
        "ledger": [
            sha(LEDGER / f)
            for f in sorted(os.listdir(LEDGER))
            if f.startswith("k5-" if a.ids[0].endswith("-r1") else "k5r2-")
        ],
        "code": code_versions(runs),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["k1", "k2", "k3", "k4", "k5"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        summary = globals()[a.scenario](conn, a)
    pre = LEDGER / f"{a.scenario}-pre.json"
    if pre.exists():
        summary["lab_readings_before"] = json.loads(pre.read_text())
    if not (LEDGER / f"{a.scenario}-pre.json").exists() and a.scenario == "k1":
        summary["lab_readings_before"] = json.loads(
            (LEDGER / "k0-pre.json").read_text()
        )
    summary["verdict"] = (
        "PASS" if all(r["pass"] for r in summary["assertions"]) else "FAIL"
    )
    summary["category"] = (
        "bounded real run (kind lab, real DeepSeek); not production proof"
    )
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(iso(summary), indent=2, sort_keys=True, default=str) + "\n"
    if re.search(r"/Users/|sk-[A-Za-z0-9]{8}|lsv2_", text):
        raise SystemExit("summary contains a local path or credential-shaped string")
    Path(a.out).write_text(text)
    print(
        json.dumps(
            {
                "scenario": a.scenario,
                "verdict": summary["verdict"],
                "assertions": [
                    (r["assertion"][:70], r["pass"]) for r in summary["assertions"]
                ],
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
