"""Blind-label driver (#295): one fresh ``claude -p`` process per item per labeller.

Each process gets only the frozen ``instructions.md`` and one item of the
frozen ``pack.json``: no tools, no MCP servers, no session persistence, an
empty working directory and a replacement system prompt. The pack (about
1.4 MB) does not fit one context, and the instructions ask for every item to
be labelled on its own. Raw replies stay in the private directory; only the
assembled label file is passed to ``scripts.eval_judge_recal labels``.

    python3 label_driver.py <model> <labeller-name> [--only R001,R002] [--jobs N]
    python3 label_driver.py --assemble <labeller-name>  # after both runs finish
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import time
from pathlib import Path

BASE = Path.home() / ".agent/eval-probes/f1-judge-recal-2026-11"
PACK = BASE / "pack-v1/pack"
WORK = BASE / "labelling"
CWD = WORK / "empty-cwd"
SYSTEM = (
    "You are an independent human-style labeller. You receive labelling "
    "instructions and one item. Follow the instructions exactly. Use nothing "
    "but the text given. Finish your reply with one line of JSON only: "
    '{"label": "pass" or "fail", "note": "<one short sentence>"}'
)
LINE = re.compile(r'\{[^{}]*"label"\s*:\s*"(pass|fail)"[^{}]*\}')


def prompt(instructions: str, item: dict) -> str:
    return (
        instructions.strip()
        + "\n\n---\n\nThis message contains exactly one item of `pack.json`. "
        "Label it. Do not produce label-template.json; reply with your reasoning, "
        "if any, and end with the single JSON line described in the system prompt."
        "\n\n```json\n" + json.dumps(item, ensure_ascii=False, indent=1) + "\n```\n"
    )


def one(model: str, out_dir: Path, instructions: str, item: dict) -> tuple[str, str]:
    dest = out_dir / f"{item['id']}.json"
    if dest.exists():
        done = json.loads(dest.read_text())
        if done.get("label") in ("pass", "fail"):
            return item["id"], done["label"]
    last = "NO_ATTEMPT"
    for attempt in range(3):
        t0 = time.time()
        try:
            r = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    model,
                    "--tools",
                    "",
                    "--strict-mcp-config",
                    "--no-session-persistence",
                    "--output-format",
                    "json",
                    "--system-prompt",
                    SYSTEM,
                ],
                input=prompt(instructions, item),
                capture_output=True,
                text=True,
                cwd=CWD,
                timeout=900,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last = "TIMEOUT"
            continue
        try:
            res = json.loads(r.stdout)
        except ValueError:
            last = f"BAD_STDOUT rc={r.returncode}"
            continue
        text = res.get("result") or ""
        matches = list(LINE.finditer(text))
        if res.get("is_error") or not matches:
            last = "IS_ERROR" if res.get("is_error") else "NO_LABEL"
            continue
        obj = json.loads(matches[-1].group(0))
        rec = {
            "id": item["id"],
            "label": obj["label"],
            "note": str(obj.get("note", ""))[:300],
            "model_usage": list((res.get("modelUsage") or {}).keys()),
            "cost_usd": res.get("total_cost_usd"),
            "seconds": round(time.time() - t0, 1),
            "attempt": attempt + 1,
            "raw": text,
        }
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.rename(dest)
        return item["id"], obj["label"]
    return item["id"], "ERROR:" + last


def assemble(labeller: str) -> Path:
    """Fill label-template.json from the per-item records."""
    doc = json.loads((PACK / "label-template.json").read_text())
    doc["labeller"] = labeller
    for item_id in doc["labels"]:
        rec = json.loads((WORK / labeller / f"{item_id}.json").read_text())
        doc["labels"][item_id] = rec["label"]
        if rec["note"]:
            doc["notes"][item_id] = rec["note"]
    out = WORK / f"labels-{labeller}.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    os.chmod(out, 0o600)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?")
    ap.add_argument("labeller", nargs="?")
    ap.add_argument("--only")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--assemble", metavar="LABELLER")
    a = ap.parse_args()
    if a.assemble:
        print(assemble(a.assemble))
        return
    instructions = (PACK / "instructions.md").read_text()
    items = json.loads((PACK / "pack.json").read_text())["items"]
    if a.only:
        keep = set(a.only.split(","))
        items = [i for i in items if i["id"] in keep]
    out_dir = WORK / a.labeller
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    results = {}
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for item_id, label in ex.map(
            lambda it: one(a.model, out_dir, instructions, it), items
        ):
            results[item_id] = label
            print(item_id, label, flush=True)
    errs = [i for i, lab in results.items() if lab.startswith("ERROR")]
    print("DONE", len(results), "errors", len(errs), errs)


if __name__ == "__main__":
    main()
