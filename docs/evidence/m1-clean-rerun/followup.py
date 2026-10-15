"""Submit one workbench follow-up on an incident as the lab reviewer (not product code).

    followup.py <incident_id> <text> <out.json>

Reads the Basic Auth user from ui-user next to this file (never printed),
reads expected_generation with the owner DSN read-only, posts the control
form, and writes the response plus the incident's Run rows afterwards.
"""

import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx2
import psycopg
from psycopg.rows import dict_row

DSN = "host=127.0.0.1 port=55592 dbname=m0_budget user=m0_lab"
BASE = "http://127.0.0.1:8080"


def read(sql, params):
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return [
            {k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)) for k, v in r.items()}
            for r in conn.execute(sql, params).fetchall()
        ]


incident, text, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
user, password = (Path(__file__).parent / "ui-user").read_text().strip().split(":", 1)
token = base64.b64encode(f"{user}:{password}".encode()).decode()
del password
(row,) = read(
    "SELECT control_generation, lifecycle, current_run_id FROM opspilot_incidents WHERE incident_id=%s",
    (incident,),
)
response = httpx2.post(
    f"{BASE}/incidents/{incident}/control",
    data={
        "action": "follow_up",
        "text": text,
        "idempotency_key": f"e2e-{uuid4().hex}",
        "expected_generation": str(row["control_generation"]),
    },
    headers={"authorization": f"Basic {token}", "origin": BASE, "accept": "application/json"},
    timeout=30.0,
)
try:
    body = response.json()
except ValueError:
    body = response.text[:500]
after = read(
    "SELECT i.control_generation, i.lifecycle, i.current_run_id, r.state"
    " FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s",
    (incident,),
)
result = {"before": row, "http_status": response.status_code, "http_body": body, "after": after}
out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(json.dumps(result))
sys.exit(0 if response.status_code == 200 else 1)
