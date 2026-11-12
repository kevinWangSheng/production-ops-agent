"""Precondition 1: rebuild what the formal judge run saw, from the archive,
without judging, and compare with the formal items.json and the published
summary.json. Prints counts and mismatches only (no report text)."""

import hashlib
import json
import sys
from pathlib import Path

from opspilot.evaluation.binary_judge import PROMPT_SHA256, judge_messages
from opspilot.evaluation.model import dataset_tree_hash, load_scenarios_v2
from scripts.eval_compare_scoring import holmes_attempts, opspilot_attempts

HOME = Path.home()
F = HOME / ".agent/eval-probes/f1-exam-2026-11-05/formal"
U = HOME / "eval-archive/f1-exam-2026-11-05/unpacked"


def failures(out: dict) -> list[str]:
    """Every reason the check fails; empty means precondition 1 holds."""
    found = []
    for name, rep in out["datasets"].items():
        for flag in (
            "dataset_hash_matches_published",
            "items_sha256_matches_published",
        ):
            if rep.get(flag) is not True:
                found.append(f"{name}:{flag}")
        if rep.get("items_prompt_sha256") != out.get("prompt_sha256_current"):
            found.append(f"{name}:prompt_sha256")
        if rep.get("published_not_in_items"):
            found.append(f"{name}:published_not_in_items")
        if rep.get("mismatches"):
            found.append(f"{name}:mismatches")
        if rep.get("judged") != rep.get("published_judged"):
            found.append(f"{name}:judged_count")
    return found


def rebuild(REPO: Path) -> dict:
    DATASETS = {
        "dev": REPO / "evals/datasets/dev-v2",
        "holdout": HOME / ".agent/eval-probes/holdout-7ec15a/pack/holdout-7ec15a",
    }
    published = json.loads(
        (REPO / "docs/evidence/f1-s10-scoring-2026-11-06/summary.json").read_text()
    )
    pub_attempts = {}
    for row in published["per_scenario"]:
        for a in row["attempts"]:
            pub_attempts[a["attempt_id"]] = (
                row["dataset"],
                row["system"],
                row["scenario_id"],
                a,
            )
    pub_rulings = {
        d["attempt_id"]: d["ruling"] for d in published["disagreements"]["items"]
    }

    out = {"prompt_sha256_current": PROMPT_SHA256, "datasets": {}}
    for name, root in DATASETS.items():
        rep = {}
        rep["dataset_hash"] = f"{root.name}:{dataset_tree_hash(root)}"
        rep["dataset_hash_matches_published"] = (
            rep["dataset_hash"].split(":")[1]
            == published["datasets"][name]["dataset"].split(":")[1]
        )
        data = {s.scenario_id: (s, a) for s, a in load_scenarios_v2(root)}
        items_path = F / f"judge-{name}/items.json"
        rep["items_sha256"] = hashlib.sha256(items_path.read_bytes()).hexdigest()
        rep["items_sha256_matches_published"] = (
            rep["items_sha256"] == published["inputs_sha256"][f"items-{name}"]
        )
        items = json.loads(items_path.read_text())
        rep["items_prompt_sha256"] = items["judge_config"]["prompt_sha256"]
        rebuilt = opspilot_attempts(
            data,
            json.loads((F / f"opspilot-{name}-round.json").read_text()),
            F / "evroot",
            recording_root=U / "opspilot/tmp/eval",
        ) + holmes_attempts(
            data,
            json.loads((F / f"holmes-{name}-round.json").read_text()),
            U / "opspilot/tmp/eval",
        )
        rb = {a["attempt_id"]: a for a in rebuilt}
        judged = [x for x in items["attempts"] if x["verdicts"]]
        rep["judged"] = len(judged)
        mism = []
        for x in judged:
            aid = x["attempt_id"]
            r = rb.get(aid)
            if r is None:
                mism.append((aid, "not_rebuilt"))
                continue
            for k in (
                "report_text",
                "expected_elements",
                "polarity",
                "category",
                "system",
                "scenario_id",
            ):
                if r[k] != x[k]:
                    mism.append((aid, f"{k}_differs"))
            _s, answer = data[x["scenario_id"]]
            h = hashlib.sha256(
                json.dumps(
                    judge_messages(answer, r["report_text"]), sort_keys=True
                ).encode()
            ).hexdigest()
            if h != x["prompt_sha256"]:
                mism.append((aid, "prompt_sha256_differs"))
            p = pub_attempts.get(aid)
            if p is None:
                mism.append((aid, "not_in_published"))
                continue
            pds, psys, psid, pa = p
            if (pds, psys, psid) != (name, x["system"], x["scenario_id"]):
                mism.append((aid, "published_identity_differs"))
            if pa["verdicts"] != x["verdicts"]:
                mism.append((aid, "published_verdicts_differ"))
            disputed = len(set(x["verdicts"].values())) > 1
            if disputed:
                if pa.get("ruling") is None or pub_rulings.get(aid) != pa["ruling"]:
                    mism.append((aid, "ruling_missing_or_differs"))
                final = pa["ruling"] == "pass"
            else:
                final = next(iter(x["verdicts"].values()))
            if pa["pass"] != final:
                mism.append((aid, "published_pass_differs"))
            if any(v is None for v in x["verdicts"].values()) or any(
                (x.get("errors") or {}).values()
            ):
                mism.append((aid, "invalid_or_error"))
        # every published judged attempt of this dataset is in items
        pub_judged = {
            aid
            for aid, (ds, _s, _i, a) in pub_attempts.items()
            if ds == name and any(v is not None for v in a["verdicts"].values())
        }
        rep["published_judged"] = len(pub_judged)
        rep["published_not_in_items"] = sorted(
            pub_judged - {x["attempt_id"] for x in judged}
        )
        rep["mismatches"] = mism
        out["datasets"][name] = rep
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    problems = failures(rebuild(Path(sys.argv[1])))
    if problems:
        print(json.dumps({"failures": problems}), file=sys.stderr)
        raise SystemExit(1)
