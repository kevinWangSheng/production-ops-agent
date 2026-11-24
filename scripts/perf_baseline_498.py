#!/usr/bin/env python3
"""#498 只读分析：从已冻结的 summary.json 算出报告耗时与 traces_search 超上限次数。

口径：事故 created_at -> conclusion_step.observed_at（均为产品内 PG 时钟）。
没有结论步的 Run 单独计数。只读 stdlib，不碰产品代码。
用法：python3 scripts/perf_baseline_498.py docs/evidence
"""
import collections, glob, json, math, os, sys
from datetime import datetime

GROUPS = [  # (名称, glob 相对 evidence 目录)
    ("11-20 Holmes 题集 OpsPilot", "rental-2026-11-20/full/opspilot/**/summary.json"),
    ("11-20 SREGym", "rental-2026-11-20/full/sregym/**/summary.json"),
    ("11-22 重跑 Holmes 题集", "rental-2026-11-20/rerun-2026-11-22/opspilot/**/summary.json"),
    ("11-22 重跑 SREGym", "rental-2026-11-20/rerun-2026-11-22/sregym/**/summary.json"),
    ("11-23 Holmes 题集 OpsPilot", "rental-2026-11-23/full/opspilot/**/summary.json"),
    ("11-23 SREGym", "rental-2026-11-23/full/sregym/**/summary.json"),
]


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def ts(s):
    return datetime.fromisoformat(s)


def analyse(paths):
    secs, nconc, steps, gaps = [], 0, [], []
    tool = collections.Counter()
    big_runs = set()
    for p in paths:
        run = json.load(open(p)).get("run") or {}
        cs = run.get("conclusion_step") or {}
        created = (run.get("incident") or {}).get("created_at")
        if cs.get("observed_at") and created:
            secs.append((ts(cs["observed_at"]) - ts(created)).total_seconds())
        else:
            nconc += 1
        st = run.get("steps") or []
        steps.append(len(st))
        obs = [ts(s["observed_at"]) for s in st if s.get("observed_at")]
        gaps += [(b - a).total_seconds() for a, b in zip(obs, obs[1:])]
        for t in run.get("tool_requests") or []:
            tool[("calls", t["tool"])] += 1
            if t.get("reason") == "RESULT_TOO_LARGE":
                tool[("big", t["tool"])] += 1
                if t["tool"] == "traces_search":
                    big_runs.add(run.get("run_id"))
            if t.get("reason") == "SOURCE_UNAVAILABLE":
                tool[("unavail", t["tool"])] += 1
    return dict(n=len(paths), concl=len(secs), noconc=nconc, secs=secs, steps=steps,
                gaps=gaps, tool=tool, big_runs=big_runs)


def main(ev):
    allsecs, rows = [], []
    for name, g in GROUPS:
        paths = sorted(glob.glob(os.path.join(ev, g), recursive=True))
        r = analyse(paths)
        r["name"] = name
        rows.append(r)
    print("| 组 | Run 数 | 有结论步 | p50 s | p95 s | max s | 步数中位 | 步间隔 p50 s | traces_search 调用 | 超上限 | 超上限 Run | SOURCE_UNAVAILABLE 调用 | 全部工具调用 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    def line(name, rs):
        secs = sum((r["secs"] for r in rs), []); steps = sum((r["steps"] for r in rs), [])
        gaps = sum((r["gaps"] for r in rs), [])
        tool = sum((r["tool"] for r in rs), collections.Counter())
        big = set().union(*[r["big_runs"] for r in rs])
        calls = sum(v for (k, _), v in tool.items() if k == "calls")
        un = sum(v for (k, _), v in tool.items() if k == "unavail")
        print(f"| {name} | {sum(r['n'] for r in rs)} | {len(secs)} | {pct(secs,.5):.0f} | {pct(secs,.95):.0f} | {max(secs, default=float('nan')):.0f} | {pct(steps,.5):.0f} | {pct(gaps,.5):.1f} | {tool[('calls','traces_search')]} | {tool[('big','traces_search')]} | {len(big)} | {un} | {calls} |")
    for r in rows:
        line(r["name"], [r])
    line("合计 Holmes 题集（11-20 + 11-23）", [rows[0], rows[4]])
    line("合计 SREGym（11-20 + 11-23）", [rows[1], rows[5]])
    line("合计全部", rows)
    print()
    tool = sum((r["tool"] for r in rows), collections.Counter())
    print("超上限（RESULT_TOO_LARGE）按工具：",
          {t: (tool[("big", t)], tool[("calls", t)]) for (k, t) in sorted(tool) if k == "big"})
    un = sorted(((v, t) for (k, t), v in tool.items() if k == "unavail"), reverse=True)[:8]
    print("SOURCE_UNAVAILABLE 前 8 工具：", un)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/evidence")
