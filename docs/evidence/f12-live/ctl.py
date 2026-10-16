"""Lab helper (not product code): post one workbench control or save a page.

    ctl.py act <incident> <action> <label> [--text T] [--revision R]
    ctl.py page <incident> <label>
Reads Basic Auth from ui-user (never printed); expected_generation read
read-only with the owner DSN. Writes <label>.json / <label>.html into out/.
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
DSN = "host=127.0.0.1 port=55593 dbname=m0_budget user=m0_lab"
BASE = "http://127.0.0.1:8080"
user, pw = (HERE / "ui-user").read_text().strip().split(":", 1)
AUTH = {
    "authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
    "origin": BASE,
}
del pw


def read(sql, params):
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        c.execute("SET TRANSACTION READ ONLY")
        return [
            {
                k: (v if isinstance(v, (int, float, bool, type(None))) else str(v))
                for k, v in r.items()
            }
            for r in c.execute(sql, params).fetchall()
        ]


def state(inc):
    return {
        "incident": read(
            "SELECT control_generation,lifecycle,state,mode,current_run_id,conclusion IS NOT NULL AS concluded FROM opspilot_incidents WHERE incident_id=%s",
            (inc,),
        ),
        "runs": read(
            "SELECT run_id,state,control_generation FROM opspilot_runs WHERE incident_id=%s ORDER BY control_generation,run_id",
            (inc,),
        ),
        "sessions": read(
            "SELECT session_id,state,ended_reason,authorization_source FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY authorized_at",
            (inc,),
        ),
    }


p = argparse.ArgumentParser()
p.add_argument("cmd")
p.add_argument("incident")
p.add_argument("a")
p.add_argument("b", nargs="?")
p.add_argument("--text")
p.add_argument("--revision")
a = p.parse_args()
# Engineer script: the local send time of the request, not a product clock.
at = datetime.now(timezone.utc).isoformat()  # noqa: TID251
if a.cmd == "page":
    r = httpx2.get(
        f"{BASE}/incidents/{a.incident}",
        headers={**AUTH, "accept": "text/html"},
        timeout=30,
    )
    (OUT / f"{a.a}.html").write_text(r.text)
    print(
        json.dumps(
            {"at": at, "page": a.a, "status": r.status_code, "bytes": len(r.text)}
        )
    )
    sys.exit(0)
action, label = a.a, a.b
before = state(a.incident)
data = {
    "action": action,
    "idempotency_key": f"f12-live-{uuid4().hex}",
    "expected_generation": str(before["incident"][0]["control_generation"]),
}
if a.text:
    data["text"] = a.text
if a.revision:
    data["revision"] = a.revision
r = httpx2.post(
    f"{BASE}/incidents/{a.incident}/control",
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
    "action": action,
    "text": a.text,
    "revision": a.revision,
    "http_status": r.status_code,
    "http_body": body,
    "before": before,
    "after": state(a.incident),
}
(OUT / f"{label}.json").write_text(json.dumps(res, indent=2, sort_keys=True) + "\n")
print(
    json.dumps(
        {
            "at": at,
            "action": action,
            "status": r.status_code,
            "body": body,
            "after": res["after"]["incident"],
            "runs": [(x["run_id"][:8], x["state"]) for x in res["after"]["runs"]],
        }
    )
)
