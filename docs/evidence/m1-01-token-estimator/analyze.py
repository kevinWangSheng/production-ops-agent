import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from opspilot.investigation.context import estimate_tokens  # noqa: E402
from opspilot.tools.registry import canonical  # noqa: E402
from opspilot.tools.tokens import count_tokens  # noqa: E402

root = Path(__file__).parent
# live-results.json already carries the prior-live-* rows from
# live-results-initial.json; key by case so a rerun never duplicates them.
_rows = {}
for name in ("live-results-initial.json", "live-results.json"):
    for row in json.loads((root / name).read_text()):
        _rows[row["case"]] = row
# prior-live-03 re-sent the exact prior-live-01 request (same request_sha256);
# count each distinct request once. The prior-live-* target_tokens labels are
# not the sizes actually sent (the first script's filler ignored the target;
# every prior request was ~366k tokens), so only measured fields are used.
_unique = {}
for row in _rows.values():
    _unique.setdefault(row["request_sha256"], row)
live = list(_unique.values())
for r in live:
    r["old_over_prompt"] = r["old_estimate_tokens"] / r["prompt_tokens"]
    r["tokenizer_over_prompt"] = r["tokenizer_content_tokens"] / r["prompt_tokens"]
    r["delta_pct"] = (
        (r["tokenizer_content_tokens"] - r["prompt_tokens"]) / r["prompt_tokens"] * 100
    )
    r["overhead_per_message"] = (
        r["prompt_tokens"] - r["tokenizer_content_tokens"]
    ) / r["message_count"]
(root / "live-results.json").write_text(json.dumps(live, indent=2) + "\n")
# Offline rebuilt rows
manifest = json.loads((root / "rebuilt-20261001" / "manifest.json").read_text())
off = []
for e in manifest:
    d = root / "rebuilt-20261001" / e["case"].replace("@", "-at")
    msgs = json.loads((d / "messages.json").read_text())
    tools = json.loads((d / "tools.json").read_text())
    content = sum(count_tokens(canonical(dict(m))) for m in msgs) + (
        count_tokens(canonical([dict(t) for t in tools])) if tools else 0
    )
    old = estimate_tokens(msgs, tools)
    e.update(
        {
            "old_estimate_tokens": old,
            "tokenizer_content_tokens": content,
            "tokenizer_message_tokens": sum(
                count_tokens(canonical(dict(m))) for m in msgs
            ),
            "tokenizer_tools_tokens": count_tokens(canonical([dict(t) for t in tools]))
            if tools
            else 0,
            "prompt_tokens": e["recorded_prompt_tokens"],
            "hash_match": e["request_sha256"] == e["recorded_request_sha256"],
        }
    )
    e["tokenizer_over_prompt"] = content / e["prompt_tokens"]
    e["delta_pct"] = (content - e["prompt_tokens"]) / e["prompt_tokens"] * 100
    e["overhead_per_message"] = (e["prompt_tokens"] - content) / e["message_count"]
    off.append(e)
(root / "offline-results.json").write_text(json.dumps(off, indent=2) + "\n")
# Fit alpha to prompt ~= tokenizer content + alpha * messages; tools fixed included in content.
vals = [r["overhead_per_message"] for r in live + off]
alpha = statistics.median(vals)
res = []
for r in live + off:
    pred = r["tokenizer_content_tokens"] + alpha * r["message_count"]
    res.append((pred - r["prompt_tokens"]) / r["prompt_tokens"] * 100)
    r["fitted_prompt_tokens"] = round(pred)
    r["fitted_delta_pct"] = (pred - r["prompt_tokens"]) / r["prompt_tokens"] * 100
summary = {
    "live_count": len(live),
    "offline_count": len(off),
    "live_prompt_total": sum(r["prompt_tokens"] for r in live),
    "live_completion_total": sum(r["completion_tokens"] for r in live),
    "fitted_message_overhead_median": alpha,
    "fitted_residual_pct_min": min(res),
    "fitted_residual_pct_max": max(res),
    "fitted_abs_max_pct": max(map(abs, res)),
    "raw_tokenizer_delta_pct_min": min(r["delta_pct"] for r in live + off),
    "raw_tokenizer_delta_pct_max": max(r["delta_pct"] for r in live + off),
}
(root / "reconciliation.json").write_text(
    json.dumps({"summary": summary, "live": live, "offline": off}, indent=2) + "\n"
)
print(json.dumps(summary, indent=2))
