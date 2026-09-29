"""Second stage: what if the product's forced retry also named numbers that the
cited views do not contain?

usage: M0_ECLASS... .venv/bin/python replay_repair.py [--dry-run]

For every base/metric_note sample whose report has a cross-view number finding
(``provenance.check``: the number is absent from the cited views but present in
another delivered view), build the product's forced-final shape

    messages up to the sample's request
    + assistant(the sample's reply)
    + FINAL_REPORT_INSTRUCTION
    + REPORT_RETRY_TEMPLATE-style feedback naming claim, number and the
      delivered views that do contain it
    + run coverage message

send it once (JSON mode, no tools, same model) and score the corrected report
with the same check. Nothing else changes. The feedback wording is this
experiment's own (the product has no such check); it follows
``REPORT_RETRY_TEMPLATE`` so the only difference from C2 is the reason.

Inputs and outputs live under $ECLASS_WORK (samples/, requests/); results go to
samples/repair-<sample>/ and ledger.jsonl (same accounting as replay_eclass).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provenance as P  # noqa: E402
import rebuild_offline as R  # noqa: E402
import replay_eclass as E  # noqa: E402

from opspilot.investigation.client import DeepSeekClient  # noqa: E402
from opspilot.investigation.loop import (  # noqa: E402
    ModelCall,
    ModelError,
    serialized_request,
)
from opspilot.investigation.reports import (  # noqa: E402
    FINAL_REPORT_INSTRUCTION,
    REPORT_RETRY_TEMPLATE,
    run_coverage_message,
)

B = "docs/evidence/m1-01-alignment-c-effect"
# request key -> (ledger case, rebuild step index or None for the last step)
SHAPES = {
    "fault-1": ("fault-1", None),
    "fault-1-at3": ("fault-1", 3),
    "normal-1": ("normal-1", None),
    "normal-1-at4": ("normal-1", 4),
    "fault-2": ("fault-2", None),
    "normal-2": ("normal-2", None),
}


def feedback(findings: list[dict], views: dict[str, dict]) -> str:
    by_claim: dict[int, list[dict]] = {}
    for f in findings:
        by_claim.setdefault(f["claim"], []).append(f)
    parts = []
    for claim, items in sorted(by_claim.items()):
        nums = ", ".join(sorted({f["number"] for f in items}))
        where = sorted(
            {
                eid
                for f in items
                for eid in views
                if eid.split(":")[0][:8] + eid.split(":")[0][-3:] in f["in_other_views"]
            }
        )[:3]
        parts.append(
            f" claim {claim}: number(s) {nums} not found in the cited view(s); "
            f"found in delivered view(s) {', '.join(where)}."
        )
    detail = "".join(parts)
    return REPORT_RETRY_TEMPLATE.format(
        reason="numbers_not_in_cited_views", detail=detail
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    work = Path(os.environ["ECLASS_WORK"])
    samples = work / "samples"
    ledger = E.Ledger(work / "ledger.jsonl")
    client = None if args.dry_run else DeepSeekClient(E.read_key())
    views_cache: dict[str, dict] = {}
    jobs = []
    for d in sorted(samples.glob("*-*-r*")):
        if d.name.startswith("repair-") or not (d / "meta.json").exists():
            continue
        m = re.match(r"(base|metric_note)-(.+)-r(\d+)$", d.name)
        if not m:
            continue
        variant, key, _ = m.groups()
        meta = json.load(open(d / "meta.json"))
        if meta.get("finish_reason") != "stop":
            continue
        text = (d / "report.txt").read_text().strip()
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text)
        try:
            report = json.loads(text)
        except ValueError:
            continue
        case, upto = SHAPES[key]
        views = views_cache.setdefault(
            case, P.views_of(json.load(open(f"{B}/{case}/ledger.json")))
        )
        findings = [f for f in P.check(report, views) if f["in_other_views"]]
        if findings:
            jobs.append((d.name, key, case, upto, text, findings, views))
    # rebuild_offline patches a module global, so do it once, before threads
    transcripts = {
        (j[2], j[3]): R.rebuild(f"{B}/{j[2]}/ledger.json", upto=j[3])["transcript"]
        for j in jobs
    }
    print(f"{len(jobs)} samples with cross-view findings")
    if args.dry_run:
        for j in jobs:
            print(j[0], [(f["claim"], f["number"]) for f in j[5]])
        return

    def run(job):
        name, key, case, upto, text, findings, views = job
        out = samples / f"repair-{name}"
        if (out / "meta.json").exists():
            return json.load(open(out / "meta.json"))
        req = work / "requests" / key
        messages = json.load(open(req / "messages.json"))
        params = json.load(open(req / "params.json"))
        transcript = transcripts[(case, upto)]
        retry = [
            *messages,
            {"role": "assistant", "content": text},
            {"role": "user", "content": FINAL_REPORT_INSTRUCTION},
            {"role": "user", "content": feedback(findings, views)},
            {"role": "user", "content": run_coverage_message(transcript.delivered)},
        ]
        call = ModelCall(
            messages=tuple(retry),
            tools=None,
            json_mode=True,
            max_tokens=params["max_tokens"],
            timeout_seconds=params["timeout_seconds"],
            model=params["model"],
        )
        if not ledger.reserve():
            raise SystemExit("MAX_CALLS reached")
        started = time.monotonic()
        entry = {
            "sample_id": f"repair-{name}",
            "variant": "repair",
            "request_bytes": len(serialized_request(call)),
        }
        out.mkdir(parents=True, exist_ok=True)
        try:
            reply = client.complete(call)
        except ModelError as exc:
            entry.update({"error": exc.code})
            ledger.append(entry)
            return entry
        entry.update(
            {
                "elapsed_s": round(time.monotonic() - started, 2),
                "finish_reason": reply.finish_reason,
                "usage": dict(reply.usage),
                "findings_before": [(f["claim"], f["number"]) for f in findings],
            }
        )
        ledger.append(entry)
        (out / "feedback.txt").write_text(feedback(findings, views))
        (out / "report.txt").write_text(reply.content or "")
        (out / "meta.json").write_text(json.dumps(entry, indent=1))
        return entry

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for e in pool.map(run, jobs):
            u = e.get("usage") or {}
            print(
                e["sample_id"],
                e.get("error") or e.get("finish_reason"),
                u.get("prompt_tokens"),
                u.get("completion_tokens"),
                flush=True,
            )


if __name__ == "__main__":
    main()
