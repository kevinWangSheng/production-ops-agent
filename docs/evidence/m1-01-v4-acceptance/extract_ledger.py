"""Export one incident's durable rows from the lab PostgreSQL as evidence files.

usage: extract_ledger.py <incident_id> <out_dir>

Writes ledger.json (incident, runs, steps, events, evidence with raw digest
only, tool charges, token/usage totals), report.json (the parsed final report
plus its sha256 and the raw report text) and events.jsonl. Model responses'
``reasoning_content`` is replaced by a fixed placeholder (C3 §12: private
protocol fields never leave the provider/Run); raw evidence bytes are kept in
the database and only their sha256/length are exported.
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

DSN = "host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab"
PLACEHOLDER = "[reasoning_content withheld: private protocol field, C3 §12]"


def scrub(obj):
    if isinstance(obj, dict):
        return {
            k: (PLACEHOLDER if k == "reasoning_content" and v else scrub(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def default(o):
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (bytes, memoryview)):
        return {"sha256": hashlib.sha256(bytes(o)).hexdigest(), "bytes": len(bytes(o))}
    raise TypeError(type(o))


def main():
    incident_id, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(DSN, row_factory=dict_row) as conn:

        def q(sql, *a):
            return [dict(r) for r in conn.execute(sql, a).fetchall()]

        incident = q(
            "SELECT * FROM opspilot_incidents WHERE incident_id=%s", incident_id
        )
        runs = q(
            "SELECT * FROM opspilot_runs WHERE incident_id=%s ORDER BY deadline",
            incident_id,
        )
        run_ids = [str(r["run_id"]) for r in runs]
        steps = q(
            "SELECT * FROM opspilot_steps WHERE run_id = ANY(%s::uuid[]) ORDER BY run_id, sequence",
            run_ids,
        )
        events = q(
            "SELECT * FROM opspilot_subject_events WHERE subject_id=%s ORDER BY sequence",
            incident_id,
        )
        evidence = q(
            "SELECT evidence_id, run_id, subject_id, status, adopted, raw_sha256, length(raw) AS raw_bytes, encode(sha256(raw),'hex') AS raw_sha256_recomputed, view, view_sha256, projection_revision, observed_at, data_as_of, committed FROM opspilot_evidence WHERE run_id = ANY(%s::text[]) ORDER BY observed_at",
            run_ids,
        )
        charges = q(
            "SELECT * FROM opspilot_tool_charges WHERE run_id = ANY(%s::uuid[]) ORDER BY epoch, operation_id",
            run_ids,
        )
        reservations = q(
            "SELECT * FROM opspilot_budget_reservations WHERE run_id = ANY(%s::uuid[])",
            run_ids,
        )
        inputs = q(
            "SELECT * FROM opspilot_inputs WHERE incident_id=%s ORDER BY sequence",
            incident_id,
        )
        controls = q(
            "SELECT * FROM opspilot_controls WHERE incident_id=%s ORDER BY created_at",
            incident_id,
        )
    # view sha256 recheck (canonical JSON as the store hashes it is not known here;
    # we record the stored hash and leave the recomputation to the reviewer's query).
    usage = []
    report = None
    for s in steps:
        resp = s.get("response") or {}
        u = resp.get("usage")
        if isinstance(u, dict):
            usage.append(
                {
                    "step_id": str(s["step_id"]),
                    "logical_key": s["logical_key"],
                    **{
                        k: u.get(k)
                        for k in (
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                            "prompt_cache_hit_tokens",
                            "prompt_cache_miss_tokens",
                        )
                    },
                    "completion_tokens_details": u.get("completion_tokens_details"),
                }
            )
        if resp.get("kind") == "conclusion":
            c = resp.get("conclusion") or {}
            text = c.get("report_content")
            parsed = None
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
            report = {
                "run_id": str(s["run_id"]),
                "step_id": str(s["step_id"]),
                "logical_key": s["logical_key"],
                "observed_at": s["observed_at"].isoformat(),
                "execution": c.get("execution"),
                "handoff": c.get("handoff"),
                "handoff_reasons": c.get("handoff_reasons"),
                "rounds": c.get("rounds"),
                "model_requests_used": c.get("model_requests_used"),
                "model_seconds_used": c.get("model_seconds_used"),
                "evidence_ids": c.get("evidence_ids"),
                "report_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()
                if isinstance(text, str)
                else None,
                "report_bytes": len(text.encode("utf-8"))
                if isinstance(text, str)
                else None,
                "report_text": text,
                "report_parsed": parsed,
                "conclusion_other_fields": {
                    k: v
                    for k, v in c.items()
                    if k not in ("report_content", "evidence_ids")
                },
            }
    totals = {
        "model_requests_with_usage": len(usage),
        "prompt_tokens": sum(u["prompt_tokens"] or 0 for u in usage),
        "completion_tokens": sum(u["completion_tokens"] or 0 for u in usage),
        "prompt_cache_hit_tokens": sum(
            u["prompt_cache_hit_tokens"] or 0 for u in usage
        ),
        "reasoning_tokens": sum(
            ((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            for u in usage
        ),
    }
    ledger = {
        "exported_at": datetime.now().astimezone().isoformat(),  # noqa: TID251 (engineer script, not product code)
        "dsn": DSN,
        "incident": incident,
        "runs": runs,
        "inputs": scrub(inputs),
        "controls": controls,
        "steps": scrub(steps),
        "events": events,
        "evidence": evidence,
        "tool_charges": charges,
        "budget_reservations": reservations,
        "usage_per_request": usage,
        "usage_totals": totals,
    }
    (out / "ledger.json").write_text(
        json.dumps(ledger, indent=1, ensure_ascii=False, default=default)
    )
    (out / "report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False, default=default)
    )
    with (out / "events.jsonl").open("w") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False, default=default) + "\n")
    print(
        json.dumps(
            {
                "runs": [
                    (r["run_id"], r["state"], r["epoch"], r["tool_operations_used"])
                    for r in runs
                ],
                "steps": len(steps),
                "events": len(events),
                "evidence": len(evidence),
                "charges": len(charges),
                "usage_totals": totals,
                "report": None
                if not report
                else {
                    k: report[k]
                    for k in (
                        "execution",
                        "handoff",
                        "handoff_reasons",
                        "rounds",
                        "model_requests_used",
                        "report_sha256",
                    )
                },
                "evidence_raw_hash_mismatch": [
                    e["evidence_id"]
                    for e in evidence
                    if e["raw_sha256"] != e["raw_sha256_recomputed"]
                ],
            },
            default=default,
            indent=1,
        )
    )


main()
