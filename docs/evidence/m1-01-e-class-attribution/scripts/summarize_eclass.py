"""Aggregate replay samples into summary.json and print the README tables.

usage: ECLASS_WORK=<dir> OUT=<evidence dir> python summarize_eclass.py

Inputs: samples/<variant>-<case>-r<n>/{meta.json,report.txt}, ledger.jsonl.
The UNSET misread labels are a manual reading (see UNSET_MISREAD): the claims
are printed by unset_scan.py and copied to unset-adjudication.json so a
reviewer can re-label them blind.
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_samples as C  # noqa: E402
import provenance as P  # noqa: E402

# (variant, case, rep) -> claim indexes read as "UNSET count used as if it were
# a count of successful calls" (E5/E6 pattern). Manual reading, not blind.
UNSET_MISREAD = {
    ("base", "fault-1-at3", 1): [18],
    ("base", "fault-1-at3", 11): [9],
    ("base", "fault-1-at3", 15): [10],
    ("base", "fault-2", 7): [9],
    ("base", "fault-2", 16): [14],
}

work = Path(os.environ["ECLASS_WORK"])
views = {}
for case in ("normal-1", "normal-2", "fault-1", "fault-2"):
    views[case] = P.views_of(json.load(open(f"{C.B}/{case}/ledger.json")))
agg = defaultdict(lambda: defaultdict(int))
detail = []
for d in sorted((work / "samples").glob("*-*-r*")):
    if d.name.startswith("repair-"):
        continue
    m = re.match(r"(base|metric_note)-(.+)-r(\d+)$", d.name)
    variant, key, rep_no = m.group(1), m.group(2), int(m.group(3))
    meta = json.load(open(d / "meta.json"))
    a = agg[(variant, key)]
    a["samples"] += 1
    a["completion_tokens"] += meta["usage"]["completion_tokens"]
    report = C.load_report(d / "report.txt") if meta["finish_reason"] == "stop" else None
    if meta["finish_reason"] != "stop":
        a["tool_calls_instead_of_report"] += 1
        continue
    if report is None:
        a["unparsable"] += 1
        continue
    a["reports"] += 1
    a["fenced"] += 1 if report.get("_fenced") else 0
    base = key.split("-at")[0]
    s = C.score(base, report, views[base])
    for k, v in s["flags"].items():
        if v and k != "E5" and k != "E6":
            a[k] += 1
    if UNSET_MISREAD.get((variant, key, rep_no)):
        a["UNSET_misread"] += 1
    cross = [f for f in s["provenance"] if f["in_other_views"]]
    if cross:
        a["cross_view_number_reports"] += 1
    detail.append(
        {
            "sample": d.name,
            "cross_view": [(f["claim"], f["number"]) for f in cross],
            "other_unmatched": [(f["claim"], f["number"]) for f in s["provenance"] if not f["in_other_views"]],
        }
    )
out = {f"{v}/{k}": dict(a) for (v, k), a in sorted(agg.items())}
Path(os.environ["OUT"], "summary.json").write_text(json.dumps({"groups": out, "detail": detail}, indent=1))
for k, a in out.items():
    print(k, a)
