"""Score the repair samples written by replay_repair.py.

usage: ECLASS_WORK=<dir> python score_repair.py

For each repair sample: does the corrected report parse; are the numbers that
were named in the feedback gone from the claims that carried them (checked on
the number string in the whole report text as well, since the model may move
it to another claim); did new cross-view numbers appear; for fault-1 (the
carried-over retry) do the reviewer-confirmed errors E2 (30261 next to
'POST /api/checkout') and E5 (UNSET compared with ERROR to argue 'not total')
survive.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_samples as C  # noqa: E402
import provenance as P  # noqa: E402

work = Path(os.environ["ECLASS_WORK"])
rows = []
views_cache = {}
for d in sorted((work / "samples").glob("repair-*")):
    meta = json.load(open(d / "meta.json"))
    src = d.name[len("repair-") :]
    m = re.match(r"(?:base|metric_note)-(.+)-r\d+$", src)
    key = m.group(1)
    case = key.split("-at")[0]
    views = views_cache.setdefault(
        case, P.views_of(json.load(open(f"{C.B}/{case}/ledger.json")))
    )
    rep = C.load_report(d / "report.txt")
    before = [n for _, n in meta["findings_before"]]
    row = {
        "sample": src,
        "case": key,
        "finish": meta["finish_reason"],
        "parsed": rep is not None,
        "named": before,
    }
    if rep is not None:
        text = json.dumps(rep, ensure_ascii=False)
        after = P.check(rep, views)
        row["named_still_in_report"] = [n for n in before if n in text.replace(",", "")]
        row["cross_view_after"] = [
            (f["claim"], f["number"]) for f in after if f["in_other_views"]
        ]
        row["other_flags_after"] = [
            (f["claim"], f["number"]) for f in after if not f["in_other_views"]
        ]
        s = C.score(case, rep, views)["flags"]
        row["flags_after"] = {k: v for k, v in s.items() if v}
        row["claims_after"] = len(rep["claims"])
    rows.append(row)
(work / "score-repair.json").write_text(json.dumps(rows, indent=1))
for r in rows:
    print(json.dumps(r))
