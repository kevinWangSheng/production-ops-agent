"""Re-apply the tightened observe.py predicates to the recorded observations.

PR #55 review adopted two tightenings of the engineer-side observation
predicate (dependency traffic required for the control window; only traces
with a span inside the window count). The observation files were recorded
before that change, so this script recomputes both predicates from the data
those files already carry (per-series values, per-trace ``any_span_in_window``)
and writes observe-recheck.json. It reads nothing live.

usage: recheck_observations.py <evidence_dir>
"""

import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
files = sorted(root.glob("*/observe-*.json")) + [root / "observe-after-restore.json"]
rows = []
for path in files:
    d = json.loads(path.read_text())
    positive = {
        n: sum(1 for s in m["series"] if s["value"] > 0)
        for n, m in d["metrics"].items()
    }
    traces = d["jaeger"]["traces"]
    in_window = [t for t in traces if t["any_span_in_window"]]
    failing = [
        t
        for t in in_window
        if t["checkout_error_spans"] and t["failed_non_checkout_spans"]
    ]
    dependency_traffic = (
        positive["checkout_calls_by_span_status"] > 0
        and positive["payment_calls_by_span_status"] > 0
        and positive["checkout_client_calls_by_peer"] > 0
    )
    rows.append(
        {
            "file": str(path.relative_to(root)),
            "window": d["window"],
            "returned_traces": len(traces),
            "traces_in_window": len(in_window),
            "traces_out_of_window": len(traces) - len(in_window),
            "positive_series_count": positive,
            "dependency_traffic": dependency_traffic,
            "control_window_ok_tightened": len({t["trace_id"] for t in in_window}) >= 2
            and dependency_traffic,
            "fault_confirmed_tightened": len(failing) >= 2,
            "recorded_precondition": d["precondition"],
        }
    )
(root / "observe-recheck.json").write_text(json.dumps(rows, indent=1))
for r in rows:
    print(
        r["file"],
        "in_window",
        f"{r['traces_in_window']}/{r['returned_traces']}",
        "dep_traffic",
        r["dependency_traffic"],
        "control_ok",
        r["control_window_ok_tightened"],
        "fault",
        r["fault_confirmed_tightened"],
        "recorded",
        r["recorded_precondition"],
    )
