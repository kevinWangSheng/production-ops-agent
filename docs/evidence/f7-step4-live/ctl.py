"""Lab helper (not product code) for the F7 step 4 live run.

    ctl.py susp <global|target> <true|false> <label>
    ctl.py act <incident> <action> <label>
    ctl.py state <label>
Basic Auth from the F12 live ui-user file (never printed); generations read
read-only with the owner DSN. Writes <label>.json into out/.
"""

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx2
import psycopg
from psycopg.rows import dict_row

HERE = Path(__file__).parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
DSN = "host=127.0.0.1 port=55597 dbname=m0_budget user=m0_lab"
BASE = "http://127.0.0.1:8080"
TARGET = "m0-otel-20260909"
UI = Path(
    "/Users/shenghuikevin/dev/AI/production-ops-agent/tmp/archive/f12-live/live/ui-user"
)
user, pw = UI.read_text().strip().split(":", 1)
AUTH = {
    "authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
    "origin": BASE,
}
del pw


def read(sql, params=()):
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        c.execute("SET TRANSACTION READ ONLY")
        return [
            {
                k: (v if isinstance(v, (int, float, bool, type(None))) else str(v))
                for k, v in r.items()
            }
            for r in c.execute(sql, params).fetchall()
        ]


def state():
    return {
        "db_now": read("SELECT clock_timestamp() AS now")[0]["now"],
        "scope": read(
            "SELECT global_generation,global_suspended FROM opspilot_scope_controls"
        ),
        "targets": read(
            "SELECT target_id,generation,suspended FROM opspilot_target_suspensions"
        ),
        "incidents": read(
            "SELECT incident_id,control_generation,lifecycle,state,mode,current_run_id,conclusion IS NOT NULL AS concluded FROM opspilot_incidents ORDER BY created_at"
        ),
        "runs": read(
            "SELECT run_id,incident_id,state,control_generation FROM opspilot_runs ORDER BY control_generation,run_id"
        ),
        "tool_charges": read("SELECT count(*) AS n FROM opspilot_tool_charges")[0]["n"],
        "evidence": read("SELECT count(*) AS n FROM opspilot_evidence")[0]["n"],
        "last_step_at": read("SELECT max(observed_at) AS t FROM opspilot_steps")[0][
            "t"
        ],
    }


p = argparse.ArgumentParser()
p.add_argument("cmd")
p.add_argument("args", nargs="*")
a = p.parse_args()
at = datetime.now(timezone.utc).isoformat()  # noqa: TID251 -- operator log time
before = state()
if a.cmd == "state":
    label = a.args[0]
    res = {"at": at, "state": before}
elif a.cmd == "susp":
    scope, value, label = a.args
    gen = (
        before["scope"][0]["global_generation"]
        if scope == "global"
        else (before["targets"][0]["generation"] if before["targets"] else 0)
    )
    data = {
        "scope": scope,
        "suspended": value,
        "expected_generation": str(gen),
        "idempotency_key": f"f7s4-{uuid4().hex}",
    }
    if scope == "target":
        data["target_id"] = TARGET
    r = httpx2.post(
        f"{BASE}/controls/suspension",
        data=data,
        headers={**AUTH, "accept": "application/json"},
        timeout=60,
    )
    try:
        body = r.json()
    except ValueError:
        body = r.text[:500]
    res = {
        "at": at,
        "request": {"scope": scope, "suspended": value, "expected_generation": gen},
        "http_status": r.status_code,
        "http_body": body,
        "before": before,
        "after": state(),
    }
elif a.cmd == "act":
    inc, action, label = a.args
    gen = next(
        i["control_generation"] for i in before["incidents"] if i["incident_id"] == inc
    )
    r = httpx2.post(
        f"{BASE}/incidents/{inc}/control",
        data={
            "action": action,
            "idempotency_key": f"f7s4-{uuid4().hex}",
            "expected_generation": str(gen),
        },
        headers={**AUTH, "accept": "application/json"},
        timeout=60,
    )
    try:
        body = r.json()
    except ValueError:
        body = r.text[:500]
    res = {
        "at": at,
        "action": action,
        "http_status": r.status_code,
        "http_body": body,
        "before": before,
        "after": state(),
    }
else:
    sys.exit("unknown command")
(OUT / f"{label}.json").write_text(json.dumps(res, indent=2, sort_keys=True) + "\n")
s = res.get("after", res.get("state"))
print(
    json.dumps(
        {
            "at": at,
            "label": label,
            "status": res.get("http_status"),
            "body": res.get("http_body"),
            "scope": s["scope"],
            "targets": s["targets"],
            "runs": [(x["run_id"][:8], x["state"]) for x in s["runs"]],
            "charges": s["tool_charges"],
            "last_step_at": s["last_step_at"],
        }
    )
)
