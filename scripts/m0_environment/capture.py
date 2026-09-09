"""Capture real lab observations. Run after the workload has started."""

import argparse
import datetime
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("label")
args = parser.parse_args()
if not args.label.replace("-", "").isalnum():
    raise SystemExit("invalid capture label")
out = ROOT / "docs/evidence/m0-real-environment" / args.label
out.mkdir(exist_ok=True)
now = datetime.datetime.now(datetime.UTC)
queries = {
    "frontend": "http://127.0.0.1:18080/",
    "prometheus-up": "http://127.0.0.1:19090/api/v1/query?query=up",
    "metric-names": "http://127.0.0.1:19090/api/v1/label/__name__/values",
    "target-info": "http://127.0.0.1:19090/api/v1/query?query=target_info",
    "trace-services": "http://127.0.0.1:16686/jaeger/ui/api/services",
    "logs-mapping": "http://127.0.0.1:19200/otel/_mapping",
    "logs-sample": "http://127.0.0.1:19200/otel/_search?size=5",
}
results = {}
for name, url in queries.items():
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            raw = response.read(2 * 1024 * 1024)
            results[name] = {"url": url, "status": response.status, "bytes": len(raw)}
            (out / (name + ".txt")).write_bytes(raw)
    except (urllib.error.URLError, TimeoutError) as exc:
        results[name] = {"url": url, "error_type": type(exc).__name__}
for name, cmd in {
    "containers": [
        "docker",
        "--context",
        "colima-m0-otel",
        "ps",
        "-a",
        "--format",
        "{{json .}}",
    ],
    "stats": [
        "docker",
        "--context",
        "colima-m0-otel",
        "stats",
        "--no-stream",
        "--format",
        "{{json .}}",
    ],
    "disk": ["df", "-h", "."],
}.items():
    process = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    (out / (name + ".txt")).write_text(process.stdout + process.stderr)
    results[name] = {"command": cmd, "exit_code": process.returncode}
(out / "capture.json").write_text(
    json.dumps({"at": now.isoformat(), "results": results}, indent=2) + "\n"
)
print(json.dumps(results, indent=2))
