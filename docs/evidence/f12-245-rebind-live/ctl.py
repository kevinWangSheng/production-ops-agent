"""Lab helper (not product code): submit one intake, post one workbench
control, or save a page, through the same routes and fields as the forms.

    ctl.py intake <target_id> <label> --text QUESTION
    ctl.py act <incident> <action> <label> [--text T] [--revision R] [--to-target UID]
    ctl.py page <incident> <label>

Reads Basic Auth from ``tmp/f245-live/ui-user`` (never printed); the expected
generation and the before/after rows are read read-only with the owner DSN.
Writes <label>.json / <label>.html into ``tmp/f245-live/out`` (raw ledger,
not committed).
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

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / "tmp" / "f245-live"
OUT = RUN / "out"
OUT.mkdir(parents=True, exist_ok=True)
DSN = "host=127.0.0.1 port=55701 dbname=f245_live user=m0_lab"
BASE = "http://127.0.0.1:18245"
user, pw = (RUN / "ui-user").read_text().strip().split(":", 1)
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
            "SELECT i.control_generation,i.lifecycle,i.state,i.mode,i.current_run_id,i.conclusion IS NOT NULL AS concluded,t.resource_uid AS target FROM opspilot_incidents i LEFT JOIN opspilot_targets t USING (target_id) WHERE incident_id=%s",
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
p.add_argument("a")
p.add_argument("b")
p.add_argument("c", nargs="?")
p.add_argument("--text")
p.add_argument("--revision")
p.add_argument("--to-target")
a = p.parse_args()
# Engineer script: the local send time of the request, not a product clock.
at = datetime.now(timezone.utc).isoformat()  # noqa: TID251
if a.cmd == "page":
    r = httpx2.get(
        f"{BASE}/incidents/{a.a}",
        headers={**AUTH, "accept": "text/html"},
        timeout=30,
    )
    (OUT / f"{a.b}.html").write_text(r.text)
    print(json.dumps({"at": at, "page": a.b, "status": r.status_code}))
    sys.exit(0)
if a.cmd == "intake":
    r = httpx2.post(
        f"{BASE}/intake/ui",
        data={
            "target_id": a.a,
            "question": a.text,
            "idempotency_key": f"f245-live-{uuid4().hex}",
        },
        headers={**AUTH, "accept": "application/json"},
        timeout=60,
    )
    res = {"at": at, "http_status": r.status_code, "http_body": r.json()}
    inc = res["http_body"].get("incident_id")
    res["after"] = state(inc) if inc else None
    (OUT / f"{a.b}.json").write_text(json.dumps(res, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"at": at, "status": r.status_code, "body": res["http_body"]}))
    sys.exit(0)
incident, action, label = a.a, a.b, a.c
before = state(incident)
data = {
    "action": action,
    "idempotency_key": f"f245-live-{uuid4().hex}",
    "expected_generation": str(before["incident"][0]["control_generation"]),
}
for field, value in (
    ("text", a.text),
    ("revision", a.revision),
    ("to_target", a.to_target),
):
    if value:
        data[field] = value
r = httpx2.post(
    f"{BASE}/incidents/{incident}/control",
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
    "request": {k: v for k, v in data.items() if k != "idempotency_key"},
    "http_status": r.status_code,
    "http_body": body,
    "before": before,
    "after": state(incident),
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
            "sessions": [
                (x["state"], x["ended_reason"]) for x in res["after"]["sessions"]
            ],
        }
    )
)
