import json, sys, pathlib, datetime as dt
root = pathlib.Path(sys.argv[1])
def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
for p in sorted(root.glob("p*/trial-*/summary.json")):
    d = json.load(open(p)); e = d["sregym_eval"]; r = e.get("result") or {}
    en = e.get("entry") or {}; t = e.get("timings") or {}
    ph = r.get("phase_seconds") or {}
    run = d.get("run") or {}
    tr = d.get("trace") or {}
    tok = None
    for k in ("tokens", "usage", "token_usage"):
        if k in run: tok = run[k]
    ms, me = ts(t.get("main_started_at")), ts(t.get("main_ended_at"))
    sub = e.get("submission") or {}
    print(json.dumps({
        "p": p.parent.parent.name, "t": p.parent.name, "status": d["status"], "cat": e["category"], "pass": e["passed"],
        "acc": r.get("accuracy"), "comp": r.get("composite_score"), "dims": r.get("dimensions"),
        "unscored": r.get("unscored_reason"), "run_status": r.get("run_status"), "incomplete": r.get("incomplete_reason"),
        "timed_out": r.get("timed_out"), "deploy_failed": r.get("deploy_failed"),
        "main_s": round((me - ms).total_seconds()) if ms and me else None,
        "phases": {k: round(v) for k, v in ph.items()}, "ttl": r.get("ttl_s") and round(r["ttl_s"]),
        "entry_exit": en.get("exit_code"), "failure": en.get("failure"), "conv": e.get("conversion_reason"),
        "sub": {k: sub.get(k) for k in ("submitted", "empty", "status_code", "error_code", "chars")},
        "run_wait": en.get("run_wait"), "obs": en.get("observability"), "term": en.get("termination"),
        "can_i_ok": all((v == "yes") == (k.startswith("list ") and "secrets" not in k) for k, v in (e.get("boundary") or {}).get("can_i", {}).items()),
        "product_path": (e.get("boundary") or {}).get("product_path"),
        "main_exit": e.get("main_exit_code"),
        "tok": {k: (run.get("tokens") or {}).get(k) for k in ("total_tokens","prompt_cache_hit_tokens","prompt_cache_miss_tokens","completion_tokens")}, "steps": len(run.get("steps") or []), "trace": [x.get("url") for x in (tr.get("roots") or [])] if isinstance(tr, dict) else None, "read_back": tr.get("read_back") if isinstance(tr, dict) else None,
    }, ensure_ascii=False))
