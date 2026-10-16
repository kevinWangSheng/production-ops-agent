"""Lab helper: hand redelivery of the stored firing alert (simulates Alertmanager's
repeat notification: same fingerprint and startsAt). Token read from file, never printed."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx2
import psycopg
from psycopg.rows import dict_row

inc, out = sys.argv[1], Path(sys.argv[2])
with psycopg.connect(
    "host=127.0.0.1 port=55593 dbname=m0_budget user=m0_lab", row_factory=dict_row
) as c:
    c.execute("SET TRANSACTION READ ONLY")
    row = c.execute(
        "SELECT delivery_id,alert_json FROM opspilot_alert_deliveries WHERE incident_id=%s AND status='firing' ORDER BY delivery_id LIMIT 1",
        (inc,),
    ).fetchone()
alert = (
    row["alert_json"]
    if isinstance(row["alert_json"], dict)
    else json.loads(row["alert_json"])
)
body = {
    "version": "4",
    "status": "firing",
    "receiver": "opspilot",
    "groupLabels": {},
    "commonLabels": alert.get("labels", {}),
    "commonAnnotations": alert.get("annotations", {}),
    "externalURL": "",
    "groupKey": "hand-redelivery",
    "truncatedAlerts": 0,
    "alerts": [alert],
}
tok = (
    Path(
        "/Users/shenghuikevin/dev/AI/production-ops-agent/tmp/m1-kind-lab/alertmanager-webhook-token"
    )
    .read_text()
    .strip()
)
# Engineer script: the local send time of the request, not a product clock.
at = datetime.now(timezone.utc).isoformat()  # noqa: TID251
r = httpx2.post(
    "http://127.0.0.1:8080/intake/alertmanager",
    content=json.dumps(body),
    headers={"authorization": f"Bearer {tok}", "content-type": "application/json"},
    timeout=30,
)
del tok
res = {
    "at": at,
    "source_delivery_id": row["delivery_id"],
    "fingerprint": alert.get("fingerprint"),
    "startsAt": alert.get("startsAt"),
    "http_status": r.status_code,
    "http_body": r.json()
    if r.headers.get("content-type", "").startswith("application/json")
    else r.text[:300],
}
out.write_text(json.dumps(res, indent=2) + "\n")
print(json.dumps(res))
