"""Render the incident page offline from a recorded real Run's ledger.

Usage (repo root): PYTHONPATH=. .venv/bin/python <this file> <ledger.json> <out.html>

The recorded conclusion text and the recorded evidence views are injected into
the in-memory workbench used by the web tests, so the page shows the real
report and the real metrics views under the real chart code. The ledger keeps
only the raw SHA-256, not the raw bytes, so raw is left empty here and the
view hash (the thing the chart reads) is verified against ``view_sha256``
before use; a mismatch aborts.
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from opspilot.tools.registry import canonical_hash
from opspilot.web.evidence import StoredEvidence
from tests.m1_web_support import basic, build_workbench, call, submit_incident

ledger = json.loads(Path(sys.argv[1]).read_text())
app, workbench, _clock = build_workbench()
incident = submit_incident(app, key="offline-1").json()["incident_id"]
subject = workbench.list_incidents()[0].incident_id
run_id = next(iter(workbench.incidents.run_ids(subject)))


class Store:
    def __init__(self, records):
        self.records = records

    def get(self, evidence_id):
        return self.records.get(evidence_id)


records = {}
for row in ledger["evidence"]:
    view = row["view"]
    assert canonical_hash(view) == row["view_sha256"], row["evidence_id"]
    records[row["evidence_id"]] = StoredEvidence(
        evidence_id=row["evidence_id"],
        run_id=run_id,
        subject_id=str(subject),
        status=row["status"],
        adopted=bool(row["adopted"]),
        raw=b"",
        raw_sha256=hashlib.sha256(b"").hexdigest(),
        view=view,
        view_sha256=row["view_sha256"],
        projection_revision=row["projection_revision"],
        observed_at=datetime.fromisoformat(row["observed_at"]),
        data_as_of=None,
    )
workbench.evidence = Store(records)
report = ledger["steps"][-1]["response"]["conclusion"]["report_content"]
workbench.incidents.incidents[subject]["conclusion"] = {
    "kind": "conclusion",
    "conclusion": {"report_content": report},
}
page = call(app, "GET", f"/incidents/{incident}", headers=basic())
assert page.status == 200
Path(sys.argv[2]).write_text(page.text)
print(
    "figures",
    page.text.count('class="evidence-chart"'),
    "unavailable",
    page.text.count("chart-unavailable"),
)
