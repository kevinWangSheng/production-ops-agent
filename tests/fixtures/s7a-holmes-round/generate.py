"""Generate a real #470 summarize round with #470's own builders (n=2).

Generated from feature/F14-s7-holmes-dataset-runner at b19e8a22 (#470).
Run from a checkout of that branch:
    python gen470.py <checkout> <fixture dir>
"""

import json
import sys
from pathlib import Path

W, F = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(W))
import tests.test_s7_holmes_dataset_impl as t  # noqa: E402

from scripts.holmes_baseline import dataset as ds  # noqa: E402

att = F / "attempts"
fo = t.fake_output
t.write_attempt(
    att,
    "dv2-2b214b",
    "h-pay-1",
    1.0,
    output=fo(result="payment Charge RPC errors cause checkout errors (trace 1a2b)"),
)
t.write_attempt(att, "dv2-2b214b", "h-pay-2", 2.0, output=fo(result="   "))
t.write_attempt(
    att,
    "dv2-8e9455",
    "h-norm-1",
    3.0,
    output=fo(result="checkout is healthy"),
    lines=("t holmes POST /api/v1/admin/tsdb/delete_series 204 1 2 0.1",),
)
t.write_attempt(att, "dv2-c39f60", "h-mf-1", 4.0, output=fo(result=None, exit_code=1))
doc = ds.summarize(W / "evals/datasets/dev-v2", att, n=2)
keep = {"dv2-2b214b", "dv2-8e9455", "dv2-c39f60", "dv2-30921c"}
doc["slots"] = [s for s in doc["slots"] if s["scenario_id"] in keep]
(F / "round.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
for s in doc["slots"]:
    print(
        s["scenario_id"],
        s["slot"],
        s["status"],
        s.get("final_state"),
        s.get("checks"),
        s.get("raw"),
        s.get("report_available"),
    )
