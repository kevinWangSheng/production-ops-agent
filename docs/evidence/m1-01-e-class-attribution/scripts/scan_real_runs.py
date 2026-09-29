"""Run provenance.check over the published report of every real Run of the four
batches and list, per Run, the numbers it flags together with whether the
independent review (review.md) mentions the same number.

usage: python scan_real_runs.py > provenance-scan.json
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provenance as P  # noqa: E402

ROOT = Path("docs/evidence")
BATCHES = {
    "6f": "m1-01-explicit-view-rerun",
    "batch1": "m1-01-limits-effect",
    "batch2": "m1-01-alignment-b-effect",
    "batch3": "m1-01-alignment-c-effect",
}
out = []
for tag, d in BATCHES.items():
    for run in sorted((ROOT / d).iterdir()):
        led, rep, rev = run / "ledger.json", run / "report.json", run / "review.md"
        if not (led.exists() and rep.exists() and rev.exists()):
            continue
        ledger = json.load(open(led))
        report = json.load(open(rep)).get("report_parsed")
        if not report:
            continue
        review = rev.read_text().replace(",", "")
        n_claims = len(report.get("claims", []))
        findings = P.check(report, P.views_of(ledger))
        for f in findings:
            f["in_review"] = f["number"] in review
        out.append(
            {
                "batch": tag,
                "run": run.name,
                "claims": n_claims,
                "flagged_numbers": len(findings),
                "flagged_claims": len({f["claim"] for f in findings}),
                "findings": findings,
            }
        )
print(json.dumps(out, indent=1))
