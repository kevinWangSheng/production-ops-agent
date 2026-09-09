"""Archive the bounded Jaeger in-memory window before stopping this lab."""

import gzip
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tmp/m0-environment/jaeger-final-export"
OUT.mkdir(exist_ok=False)
BASE = "http://127.0.0.1:16686/jaeger/ui/api"
end = time.time()
start = end - 3600
with urllib.request.urlopen(BASE + "/services", timeout=10) as response:
    services_raw = response.read(1024 * 1024)
(OUT / "services.json").write_bytes(services_raw)
services = json.loads(services_raw)["data"]
records = []
trace_ids = set()
for index, service in enumerate(services):
    params = {
        "service": service,
        "start": int(start * 1e6),
        "end": int(end * 1e6),
        "limit": 25000,
    }
    url = BASE + "/traces?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = response.read(64 * 1024 * 1024 + 1)
    path = OUT / (str(index) + ".json.gz")
    path.write_bytes(gzip.compress(raw, mtime=0))
    if len(raw) > 64 * 1024 * 1024:
        raise SystemExit(
            "Trace response exceeded bounded export; preserve partial and report incomplete"
        )
    traces = json.loads(raw)["data"]
    trace_ids.update(trace["traceID"] for trace in traces)
    records.append(
        {
            "service": service,
            "query": url,
            "raw_path": str(path.relative_to(ROOT)),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_bytes": len(raw),
            "trace_count": len(traces),
            "at_limit": len(traces) == 25000,
        }
    )
summary = {
    "start": start,
    "end": end,
    "services": records,
    "unique_trace_ids": len(trace_ids),
    "scope": "Jaeger retained one-hour window per discovered service; not a transactional snapshot or proof of no earlier eviction",
}
(ROOT / "docs/evidence/m0-real-environment/jaeger-final-export.json").write_text(
    json.dumps(summary, indent=2) + "\n"
)
print(
    f"Archived {len(trace_ids)} distinct retained traces across {len(services)} services."
)
