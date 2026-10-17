"""Freeze the F7 step 4 live run from product rows (read-only).

Windows come from opspilot_suspension_audit (database clock). Queries and
steps come from the audit projection (opspilot.acceptance_audit) and
opspilot_steps. Checks: no query recorded inside a suspension window; every
step written inside a window is a late_result; the Run stays paused after
each release until an operator resume; suspension audit rows exist.
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from opspilot.acceptance_audit import audit_timeline

DSN, INCIDENT, OUT = sys.argv[1], UUID(sys.argv[2]), sys.argv[3]
with psycopg.connect(DSN) as conn:
    conn.execute("SET TRANSACTION READ ONLY")
    timeline = audit_timeline(conn, INCIDENT)
    cur = conn.cursor(row_factory=dict_row)
    audit = cur.execute(
        "SELECT created_at,target_id IS NULL AS is_global,suspended,generation,actor FROM opspilot_suspension_audit ORDER BY created_at"
    ).fetchall()
    steps = cur.execute(
        "SELECT sequence,status,observed_at,control_generation FROM opspilot_steps ORDER BY observed_at"
    ).fetchall()
    controls = cur.execute(
        "SELECT action,created_at,actor FROM opspilot_controls WHERE incident_id=%s ORDER BY created_at",
        (INCIDENT,),
    ).fetchall()
    run = cur.execute(
        "SELECT run_id,state FROM opspilot_runs WHERE incident_id=%s", (INCIDENT,)
    ).fetchall()

windows = []
for scope in (True, False):
    rows = [a for a in audit if a["is_global"] is scope]
    for on, off in zip(rows[0::2], rows[1::2]):
        assert on["suspended"] and not off["suspended"]
        windows.append(
            {
                "scope": "global" if scope else "target",
                "on": on["created_at"],
                "off": off["created_at"],
                "generation_on": on["generation"],
                "generation_off": off["generation"],
            }
        )

queries = [e for e in timeline.entries if e.category == "query"]
resumes = [c["created_at"] for c in controls if c["action"] == "resume"]
checks = []
for w in windows:
    inside_q = [e for e in queries if w["on"] <= e.recorded_at < w["off"]]
    inside_s = [s for s in steps if w["on"] <= s["observed_at"] < w["off"]]
    next_resume = min((r for r in resumes if r > w["off"]), default=None)
    after_release = [
        s
        for s in steps
        if w["off"]
        <= s["observed_at"]
        < (next_resume or datetime.max.replace(tzinfo=timezone.utc))
    ]
    checks.append(
        {
            "window": {
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in w.items()
            },
            "queries_inside": len(inside_q),
            "steps_inside": [s["status"] for s in inside_s],
            "inside_steps_all_late_result": all(
                s["status"] == "late_result" for s in inside_s
            ),
            "queries_before_on": len([e for e in queries if e.recorded_at < w["on"]]),
            "operator_resume_after_release": next_resume.isoformat()
            if next_resume
            else None,
            "steps_between_release_and_resume": len(after_release),
            "ok": not inside_q
            and all(s["status"] == "late_result" for s in inside_s)
            and not after_release,
        }
    )
summary = {
    "frozen_at": datetime.now(timezone.utc).isoformat(),  # noqa: TID251 -- evidence freeze time, not a lease decision
    "incident_id": str(INCIDENT),
    "runs": [{"run_id": str(r["run_id"]), "state": r["state"]} for r in run],
    "suspension_audit": [
        {
            "at": a["created_at"].isoformat(),
            "scope": "global" if a["is_global"] else "target",
            "suspended": a["suspended"],
            "generation": a["generation"],
            "actor": a["actor"],
        }
        for a in audit
    ],
    "controls": [
        {"action": c["action"], "at": c["created_at"].isoformat(), "actor": c["actor"]}
        for c in controls
    ],
    "steps": [
        {
            "sequence": s["sequence"],
            "status": s["status"],
            "at": s["observed_at"].isoformat(),
            "control_generation": s["control_generation"],
        }
        for s in steps
    ],
    "query_count": len(queries),
    "audit_counts": dict(timeline.counts),
    "checks": checks,
    "ok": bool(checks) and len(checks) == 2 and all(c["ok"] for c in checks),
}
body = json.dumps(summary, indent=2, sort_keys=True) + "\n"
open(OUT, "w").write(body)
print(
    json.dumps(
        {
            "ok": summary["ok"],
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "checks": [
                (
                    c["window"]["scope"],
                    c["queries_inside"],
                    c["steps_inside"],
                    c["steps_between_release_and_resume"],
                    c["ok"],
                )
                for c in checks
            ],
        }
    )
)
