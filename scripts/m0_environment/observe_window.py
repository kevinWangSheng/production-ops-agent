"""Engineer observation of a fixed real workload window, independent of any LLM."""

import argparse
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("label")
parser.add_argument("start", type=float)
parser.add_argument("end", type=float)
args = parser.parse_args()
if not args.label.replace("-", "").isalnum() or not args.start < args.end:
    raise SystemExit("invalid label/window")
summary_path = (
    ROOT / "docs/evidence/m0-real-environment" / (args.label + "-observation.json")
)
if summary_path.exists() or summary_path.is_symlink():
    raise SystemExit("Historical observation exists; no query or files written.")
raw_dir = ROOT / "tmp/m0-environment/raw-evidence" / args.label
raw_dir.mkdir(parents=True, exist_ok=False)
queries = {
    "calls": (
        "http://127.0.0.1:19090/api/v1/query",
        {
            "query": "sum by(service_name,status_code) (increase(traces_span_metrics_calls_total[5m]))",
            "time": args.end,
        },
    ),
    "transactions": (
        "http://127.0.0.1:19090/api/v1/query",
        {
            "query": "sum(increase(app_payment_transactions_total[5m]))",
            "time": args.end,
        },
    ),
    "checkout-rpc": (
        "http://127.0.0.1:19090/api/v1/query",
        {
            "query": 'sum by(rpc_service,rpc_method,rpc_grpc_status_code) (increase(rpc_client_duration_milliseconds_count{service_name="checkout"}[5m]))',
            "time": args.end,
        },
    ),
    "latency": (
        "http://127.0.0.1:19090/api/v1/query",
        {
            "query": 'histogram_quantile(0.95, sum by(le,service_name) (rate(rpc_server_duration_milliseconds_bucket{service_name="checkout"}[5m])))',
            "time": args.end,
        },
    ),
}
for service in ["checkout", "payment"]:
    queries["traces-" + service] = (
        "http://127.0.0.1:16686/jaeger/ui/api/traces",
        {
            "service": service,
            "start": int(args.start * 1e6),
            "end": int(args.end * 1e6),
            "limit": 20,
        },
    )
queries["logs-frontend-proxy"] = (
    "http://127.0.0.1:18081/integrations/m0-otel-20260909/logs",
    {"service": "frontend-proxy", "start": args.start, "end": args.end},
)
records = []
for name, (base, params) in queries.items():
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            status, raw = response.status, response.read(10 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(8192)
    except (urllib.error.URLError, TimeoutError) as error:
        records.append({"name": name, "url": url, "error_type": type(error).__name__})
        continue
    path = raw_dir / (name + ".json")
    path.write_bytes(raw)
    record = {
        "name": name,
        "url": url,
        "status": status,
        "raw_path": str(path.relative_to(ROOT)),
        "raw_bytes": len(raw),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if len(raw) > 10 * 1024 * 1024:
        record["incomplete"] = True
        records.append(record)
        continue
    payload = json.loads(raw)
    if name.startswith("traces-") and status == 200:
        traces = payload.get("data", [])
        counts = {}
        for trace in traces:
            for span in trace["spans"]:
                service = trace["processes"][span["processID"]]["serviceName"]
                tags = {tag["key"]: tag["value"] for tag in span.get("tags", [])}
                entry = counts.setdefault(service, {"spans": 0, "error_spans": 0})
                entry["spans"] += 1
                entry["error_spans"] += int(
                    tags.get("error") is True or tags.get("otel.status_code") == "ERROR"
                )
        record.update(
            trace_ids=[trace["traceID"] for trace in traces], observed_spans=counts
        )
    elif name.startswith("logs-") and status == 200:
        hits = payload["data"]["hits"]
        record.update(
            total_hits=hits["total"],
            returned_hits=len(hits["hits"]),
            samples=[
                {
                    "timestamp": hit["_source"].get("@timestamp"),
                    "trace_id": hit["_source"].get("traceId"),
                    "body": hit["_source"].get("body"),
                }
                for hit in hits["hits"]
            ],
        )
    else:
        record["result"] = payload
    records.append(record)
summary = {
    "label": args.label,
    "start": args.start,
    "end": args.end,
    "boundary": "Developer observations; 5m metric calculations and max20 trace/log samples, not complete product health",
    "observations": records,
}
with summary_path.open("x") as stream:
    stream.write(json.dumps(summary, indent=2) + "\n")
print(
    json.dumps(
        {
            record["name"]: record.get("status", record.get("error_type"))
            for record in records
        }
    )
)
