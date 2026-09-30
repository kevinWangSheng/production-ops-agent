"""Per-Run tool-outcome summary from ledger.json: statuses, RESULT_TOO_LARGE
(token path vs byte path), view token counts, and the call that followed each
refusal. Usage: vt_summary.py <run-dir>"""

import json
import sys

sys.path.insert(0, "<worktree>")
from opspilot.tools.registry import canonical
from opspilot.tools.tokens import count_tokens

d = json.load(open(sys.argv[1] + "/ledger.json"))
rows = []
for st in d["steps"]:
    resp = st.get("response") or {}
    calls = (
        ((resp.get("assistant") or resp.get("message") or {}).get("tool_calls"))
        or resp.get("tool_calls")
        or []
    )
    for i, view in enumerate(
        [
            (t.get("result") if isinstance(t, dict) and "result" in t else t)
            for t in (st.get("tool_results") or [])
        ]
    ):
        args = None
        if i < len(calls):
            args = calls[i].get("function", {}).get("arguments")
        rows.append((st["sequence"], i, view, args))
out = {
    "tool_results": 0,
    "by_status": {},
    "refused_view_tokens": [],
    "refused_bytes": [],
    "views": [],
}
for seq, i, v, args in rows:
    out["tool_results"] += 1
    key = f"{v.get('status')}/{v.get('reason')}"
    out["by_status"][key] = out["by_status"].get(key, 0) + 1
    if v.get("reason") == "RESULT_TOO_LARGE":
        (
            out["refused_view_tokens"] if "view_tokens" in v else out["refused_bytes"]
        ).append(
            {
                "step": seq,
                "idx": i,
                "tool": v.get("tool"),
                "args": args,
                "view_tokens": v.get("view_tokens"),
                "max_view_tokens": v.get("max_view_tokens"),
                "max_result_bytes": v.get("max_result_bytes"),
            }
        )
    elif v.get("status") == "ok":
        out["views"].append(
            {
                "step": seq,
                "tool": v.get("tool"),
                "tokens": count_tokens(canonical(v)),
                "rows": v.get("returned_count"),
            }
        )
out["max_ok_view_tokens"] = max([x["tokens"] for x in out["views"]] or [0])
print(json.dumps(out, indent=1, default=str))
