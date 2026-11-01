"""#431 step 3 runner (scratch, not committed): one v2 probe round on the
kind lab through scripts.eval_live_probe.run_probe with real_deps(probe).
Usage: run_probe_round.py ROUND ROOT PG_PORT SCENARIO [SCENARIO...]
       [--otlp-url URL]
Prints probe.json path and, for every scenario with a flag, Jaeger error
span counts for ad/checkout/payment in the effective window."""
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from scripts import eval_live
from scripts.eval_live_inject import OtlpSpanWriter
from scripts.eval_live_lab import LabTelemetry, SystemClock
from scripts.eval_live_probe import run_probe

args = sys.argv[1:]
otlp = None
if "--otlp-url" in args:
    i = args.index("--otlp-url")
    otlp = args[i + 1]
    args = args[:i] + args[i + 2 :]
round_id, root, pg_port, *ids = args
root = Path(root)
deps = eval_live.real_deps(
    "http://127.0.0.1:28013", round_id, probe=True, scenarios_root=root, v2_probe=True
)
if otlp:
    deps = replace(deps, span_writer=OtlpSpanWriter(url=otlp))
slots = eval_live.plan_slots(root, ids, 1)
config = eval_live.RoundConfig(round_id, slots, "impl-431", "b431", int(pg_port), None, ("probe",), 1)
started = SystemClock().now()
result = run_probe(config, deps)
print(json.dumps({"round": round_id, "exit": result.exit_code, "reason": result.reason,
                  "slots": [(s.scenario_id, s.status, s.reason, s.closure_reason) for s in result.slots]}))
probe = eval_live.round_evidence(deps, round_id) / "probe.json"
print("probe.json", probe)
doc = json.loads(probe.read_text())
tel = LabTelemetry(SystemClock())
counts = {}
for entry in doc["scenarios"]:
    fault = entry.get("fault") or {}
    eff = fault.get("effective_at")
    if not eff:
        continue
    start = datetime.fromisoformat(eff)
    end = start + timedelta(seconds=300)
    row = {}
    for svc in ("ad", "frontend", "checkout", "payment"):
        q = tel.spans(svc, start, end, 30.0)
        ops = {}
        for sp in q.spans:
            ops[sp.operation] = ops.get(sp.operation, 0) + 1
        row[svc] = {"error_spans": len(q.spans), "complete": q.complete, "operations": ops}
    calls = tel.checkout_calls(start, end, 30.0)
    row["checkout_calls"] = {"value": calls.value, "complete": calls.complete}
    row["window"] = [start.isoformat(), end.isoformat()]
    counts[entry["scenario_id"]] = row
out = Path(sys.argv[0]).parent / f"{round_id}-jaeger.json"
out.write_text(json.dumps(counts, indent=2, sort_keys=True))
print("jaeger", out)
print(json.dumps(counts, indent=1)[:3000])
