"""Read-only replay of actual retained business raws; no model/backend/environment calls."""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.m0_environment.holmes_baseline import (  # noqa: E402
    metric_projection,
    trace_projection,
)

results = []
for run in ("m002-fault-01", "m002-normal-03"):
    folder = ROOT / "tmp/m0-environment/holmes-runs" / run
    registry = json.loads((folder / "deployment-registry.json").read_text())
    for path in sorted(folder.glob("*-raw.json")):
        before = path.read_bytes()
        raw = json.loads(before)
        if raw.get("tool") not in {"otel_traces", "otel_metrics"}:
            continue
        original = json.loads(
            (folder / (raw["evidence_id"] + "-tool-model-view.json")).read_text()
        )
        if raw["tool"] == "otel_metrics":
            assert metric_projection(raw, version="m0-02-metrics-v1") == original
            assert (
                "not a zero"
                in metric_projection(raw)["metric_semantics"][
                    "missing_series_semantics"
                ]
            )
            continue
        assert trace_projection(raw, registry, version="m0-02-v2") == original
        view = trace_projection(raw, registry)
        data = view["data"]
        traces = raw["data"]["data"]["data"]
        raw_spans = {
            (trace["traceID"], span["spanID"]): span
            for trace in traces
            for span in trace["spans"]
        }
        assert data["backend_returned_trace_count"] == len(traces)
        assert data["backend_returned_span_count"] == sum(
            len(trace["spans"]) for trace in traces
        )
        assert data["actual_visible_span_count"] == len(data["sampled_spans"])
        assert (
            data["omitted_span_count"] + data["actual_visible_span_count"]
            == data["backend_returned_span_count"]
        )
        assert data["actual_visible_trace_count"] == len(
            {s["trace_id"] for s in data["sampled_spans"]}
        )
        assert data["sampled_spans"][0]["service"] == raw["query"]["service"]
        expected_fields = 0
        for trace in traces:
            for span in trace["spans"]:
                expected_fields += sum(
                    t.get("key") in data["error_detail_coverage"]["covered_keys"]
                    for t in span.get("tags", [])
                )
                expected_fields += sum(
                    f.get("key") in data["error_detail_coverage"]["covered_keys"]
                    for log in span.get("logs", [])
                    for f in log.get("fields", [])
                )
        assert (
            data["error_detail_coverage"]["raw_detail_field_count"] == expected_fields
        )
        assert (
            data["error_detail_coverage"]["visible_detail_field_count"]
            + data["error_detail_coverage"]["omitted_detail_field_count"]
            == expected_fields
        )
        for shown in data["sampled_spans"]:
            original_span = raw_spans[(shown["trace_id"], shown["span_id"])]
            all_fields = [("span_tag", t) for t in original_span.get("tags", [])] + [
                ("span_log", f)
                for log in original_span.get("logs", [])
                for f in log.get("fields", [])
            ]
            for field in shown["error_details"]:
                assert any(
                    location == field["location"]
                    and f["key"] == field["key"]
                    and str(f["value"]).startswith(field["value"])
                    for location, f in all_fields
                )
        wire = json.dumps(view, ensure_ascii=False).encode()
        assert len(wire) <= 14000
        assert path.read_bytes() == before
        results.append(
            {
                "run": run,
                "evidence_id": raw["evidence_id"],
                "old_view_exact_replay": True,
                "raw_sha256": hashlib.sha256(before).hexdigest(),
                "new_projection": data["projection_version"],
                "query_service": raw["query"]["service"],
                "raw_traces": data["backend_returned_trace_count"],
                "raw_spans": data["backend_returned_span_count"],
                "visible_spans": data["actual_visible_span_count"],
                "visible_traces": data["actual_visible_trace_count"],
                "view_bytes": len(wire),
                "raw_detail_fields": expected_fields,
                "shown_detail_fields": data["error_detail_coverage"][
                    "visible_detail_field_count"
                ],
                "omitted_detail_fields": data["error_detail_coverage"][
                    "omitted_detail_field_count"
                ],
            }
        )
print(
    json.dumps(
        {
            "status": "offline_replay_pass",
            "real_http": 0,
            "model_quality": "not_evaluated; prior failures unchanged",
            "cases": results,
        },
        indent=2,
    )
)
