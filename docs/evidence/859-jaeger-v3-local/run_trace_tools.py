"""Run the otel-demo-ext trace tools through the product executor against a
local Jaeger 2.23 (synthetic spans). Prints only statuses, key sets, counts."""

import json
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from opspilot.persistence import Lease
from opspilot.tools import ToolRequest
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.outcomes import PROJECTION_REVISION, Window
from opspilot.tools.profiles import select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.safety.gateway_support import _Store, ext_env

base = sys.argv[1]
now = datetime.fromtimestamp(int(time.time()), UTC)
ca = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
ca.write("placeholder\n")
ca.close()
env = {**ext_env(ca.name), "OPSPILOT_OTEL_JAEGER_URL": base}
env.pop("OPSPILOT_OTEL_TOKEN", None)
profile = select_profile(env)
lease = Lease(
    incident_id=uuid4(),
    run_id=uuid4(),
    owner=uuid4(),
    epoch=1,
    control_generation=3,
    global_suspension_generation=1,
    target_suspension_generation=2,
)
clock = FakeClock(start=now)
evidence = RecordingSink()


class Store(_Store):
    def rebuild(self, incident_id):
        out = super().rebuild(incident_id)
        out["run"]["deadline"] = now + timedelta(minutes=10)
        return out


factory = profile.executor_factory(Store(lease), evidence, clock)
run_input = profile.face(clock).input_for(
    run_id=str(lease.run_id),
    question="Why is cart failing?",
    target_id=TARGET_ID,
    deadline=now + timedelta(minutes=10),
    model_requests=8,
)
ex = factory(lease, run_input)
window = Window(now - timedelta(minutes=50), now)
calls = [
    ("traces_list_services", {}),
    ("traces_list_operations", {"service": "cart"}),
    ("traces_search", {"service": "cart"}),
    (
        "traces_search_by_tags",
        {"service": "cart", "tags": 'app.retry="true"', "min_duration": "50ms"},
    ),
    ("traces_search_by_tags", {"service": "cart", "tags": 'no.such="x"'}),
    ("traces_query_trace_by_id", {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}),
    ("traces_query_trace_by_id", {"trace_id": "ffffffffffffffffffffffffffffffff"}),
]
print("projection_revision", PROJECTION_REVISION)
for i, (tool, params) in enumerate(calls):
    o = ex.execute(
        ToolRequest(
            step_id="ev",
            tool_index=i,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=params,
            window=window.as_json(),
        )
    )
    v = o.model_view or {}
    content = v.get("content")
    row = {
        "tool": tool,
        "params": sorted(params),
        "status": o.status,
        "reason": o.reason,
        "rows": len(content) if isinstance(content, list) else None,
        "backend_traces_returned": v.get("backend_traces_returned"),
        "backend_spans_returned": v.get("backend_spans_returned"),
        "trace_found": v.get("trace_found"),
        "notes": len(v.get("notes") or []),
        "query_jaeger_api": (v.get("query") or {}).get("jaeger_api")
        if isinstance(v.get("query"), dict)
        else None,
    }
    if isinstance(content, list) and content and isinstance(content[0], dict):
        row["row_keys"] = sorted(content[0])
        if tool.startswith("traces_") and "error_by_visible_tags" in content[0]:
            row["error_spans"] = sum(
                1 for r in content if r.get("error_by_visible_tags")
            )
            row["detail_fields"] = sum(
                len(r.get("error_details") or []) for r in content
            )
            row["child_of_refs"] = sum(
                len(r.get("parent_references") or []) for r in content
            )
    if i == 0:
        print("view.query", type(v.get("query")).__name__, str(v.get("query"))[:200])
    print(json.dumps(row, sort_keys=True))
print(
    "evidence_records_with_jaeger_api_v3",
    sum(
        '"jaeger_api":"v3"' in repr(r) or "'jaeger_api': 'v3'" in repr(r)
        for r in evidence.records
    ),
    "of",
    len(evidence.records),
)
