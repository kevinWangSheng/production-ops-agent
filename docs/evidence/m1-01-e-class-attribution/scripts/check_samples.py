"""Score replay samples against the six third-batch e-class errors.

usage: ECLASS_WORK=<dir> python check_samples.py <variant> [--show]

Deterministic predicates (per error, on the parsed report of one sample):

  E1 normal-1  a claim states the cart range with maximum 27,542 (not in any
               cited view)                       -> provenance finding 27542
  E2 fault-1   'POST /api/checkout' durations given with minimum 30261 (that
               is the min of 'executing api route (pages) /api/checkout')
  E3 fault-1   successful frontend durations 128935/148885 attributed to a
               claim that does not cite a view containing them
  E4 fault-1   payment Charge success durations 9662/5386, same rule
  E5 fault-1   checkout STATUS_CODE_UNSET value compared with ERROR to argue
               that failures are not total / part of the volume is non-error
  E6 fault-2   same argument with UNSET 10.0 / 65.0

E5/E6 are semantic; the predicate is a keyword filter and every hit is
printed with --show for a human read (README lists the reading).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provenance as P  # noqa: E402

B = "docs/evidence/m1-01-alignment-c-effect"
UNSET_ARG = re.compile(
    r"(exceed|greater|more than|outnumber|higher|larger|non-error|not (a )?total|"
    r"not (a )?complete|part of|partial|majority|most of|only .{0,40}error)",
    re.I,
)


def load_report(path: Path) -> dict | None:
    text = path.read_text().strip()
    fenced = text.startswith("```")
    if fenced:  # the product's parser rejects this; scored anyway, flagged
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
    try:
        r = json.loads(text)
    except ValueError:
        return None
    if isinstance(r, dict) and "claims" in r:
        r["_fenced"] = fenced
        return r
    return None


def score(case: str, report: dict, views: dict) -> dict:
    found = P.check(report, views)
    nums = {(f["claim"], f["number"]) for f in found}
    flags: dict[str, list] = {}
    claims = report.get("claims", [])
    if case == "normal-1":
        flags["E1"] = [c for c, n in nums if n == "27542"]
    if case == "fault-1":
        flags["E2"] = [
            i
            for i, c in enumerate(claims)
            if re.search(r"POST /api/checkout[^.;]{0,160}30261|30261[^.;]{0,60}POST /api/checkout", c["text"])
        ]
        flags["E3"] = [c for c, n in nums if n in ("128935", "148885")]
        flags["E4"] = [c for c, n in nums if n in ("9662", "5386")]
        flags["E5"] = [
            i
            for i, c in enumerate(claims)
            if "UNSET" in c["text"] and ("67.5" in c["text"] or "9.94" in c["text"]) and UNSET_ARG.search(c["text"])
        ]
    if case == "fault-2":
        flags["E6"] = [
            i
            for i, c in enumerate(claims)
            if "UNSET" in c["text"] and re.search(r"\b(10\.0|65\.0|65)\b", c["text"]) and UNSET_ARG.search(c["text"])
        ]
    return {
        "flags": {k: sorted(set(v)) for k, v in flags.items()},
        "provenance": found,
    }


def main() -> None:
    variant = sys.argv[1]
    show = "--show" in sys.argv
    work = Path(os.environ["ECLASS_WORK"])
    views = {}
    for case in ("normal-1", "normal-2", "fault-1", "fault-2"):
        led = json.load(open(f"{B}/{case}/ledger.json"))
        views[case] = P.views_of(led)
    rows = []
    for d in sorted((work / "samples").glob(f"{variant}-*")):
        case = re.match(rf"{re.escape(variant)}-(.+)-r\d+", d.name).group(1)
        meta = json.load(open(d / "meta.json"))
        rep = load_report(d / "report.txt") if meta.get("finish_reason") == "stop" else None
        base = case.split("-at")[0]
        row = {"sample": d.name, "case": case, "fenced": bool(rep and rep.get("_fenced")), "finish": meta.get("finish_reason"), "report": rep is not None}
        if rep is not None:
            s = score(base, rep, views[base])
            row.update(s)
            if show:
                for k, idx in s["flags"].items():
                    for i in idx:
                        print(d.name, k, i, rep["claims"][i]["text"][:400])
        rows.append(row)
    print(json.dumps(rows, indent=0)[:0])
    out = work / f"score-{variant}.json"
    out.write_text(json.dumps(rows, indent=1))
    from collections import defaultdict

    agg = defaultdict(lambda: [0, 0, 0])  # case -> [reports, no_report, ...]
    for r in rows:
        a = agg[r["case"]]
        if r["report"]:
            a[0] += 1
        else:
            a[1] += 1
    for case, (n, none, _) in sorted(agg.items()):
        errs = {}
        for r in rows:
            if r["case"] == case and r["report"]:
                for k, v in r["flags"].items():
                    errs[k] = errs.get(k, 0) + (1 if v else 0)
        prov = sum(1 for r in rows if r["case"] == case and r["report"] and r["provenance"])
        print(f"{case}: reports={n} no_report={none} flagged={errs} provenance_flagged_reports={prov}")


if __name__ == "__main__":
    main()
