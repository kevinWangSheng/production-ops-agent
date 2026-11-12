"""Calibrate the published F1 scoring flow against blind labels (#295, B1).

The judged reports of the formal scoring run (``eval_compare_scoring``
``items.json``, one per dataset) are exported as a blind pack, labelled
pass/fail by two independent labellers, disagreements are ruled by the
user, and the final labels are compared with the published verdicts under
the thresholds frozen in ``docs/evidence/f1-judge-recal-2026-11/freeze.json``.

    # 1. Blind pack (private dir outside the repo; no labels exist yet)
    .venv/bin/python -m scripts.eval_judge_recal export \\
        --items dev=<formal>/judge-dev/items.json \\
        --items holdout=<formal>/judge-holdout/items.json \\
        --dataset dev=evals/datasets/dev-v2 --dataset holdout=<holdout dir> \\
        --published docs/evidence/f1-s10-scoring-2026-11-06/summary.json \\
        --instructions docs/evidence/f1-judge-recal-2026-11/instructions.md \\
        --out <private>/pack-v1

    # 2. After both labellers return <file A> and <file B>
    .venv/bin/python -m scripts.eval_judge_recal labels --pack <private>/pack-v1 \\
        --freeze docs/evidence/f1-judge-recal-2026-11/freeze.json \\
        --label <file A> --label <file B> --out <private>/labels-v1

    # 3. After the user fills <private>/labels-v1/adjudication.json
    .venv/bin/python -m scripts.eval_judge_recal compare --pack <private>/pack-v1 \\
        --freeze docs/evidence/f1-judge-recal-2026-11/freeze.json \\
        --merged <private>/labels-v1/merged.json \\
        --adjudication <private>/labels-v1/adjudication.json --out <result.json>

The pack never carries the system, dataset, scenario or attempt id, nor any
judge verdict or rationale; those stay in ``private/identity-map.json``. The
adjudication brief carries neither judge nor labeller opinions. ``compare``
output holds ids and counts only, never report text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import secrets
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: no system name in anything the labellers see
PACK_SCHEMA = "judge-recal-pack-1"
IDENTITY_SCHEMA = "opspilot-judge-recal-identity-1"
MANIFEST_SCHEMA = "opspilot-judge-recal-manifest-1"
LABELS_SCHEMA = "judge-recal-labels-1"
MERGED_SCHEMA = "opspilot-judge-recal-merged-1"
ADJUDICATION_SCHEMA = "opspilot-judge-recal-adjudication-1"
RESULT_SCHEMA = "opspilot-judge-recal-result-1"
FREEZE_SCHEMA = "opspilot-judge-recal-freeze-1"
ITEMS_SCHEMA = "opspilot-compare-scoring-items-1"
JUDGE_NAMES = ("glm", "openai")
LABEL_VALUES = ("pass", "fail")

#: Names replaced in report text before export: either system's name, as it
#: appears in lab cluster names, alert names and upstream doc URLs.
NAME_PATTERN = re.compile(r"holmesgpt\.dev|holmes(?:gpt)?|opspilot", re.IGNORECASE)
NAME_REPLACEMENT = "[name removed]"


class RecalError(ValueError):
    """A fixed code; never carries report text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    ).encode()


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:
        handle.write(data)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RecalError(f"FILE_MISSING:{path.name}") from None
    except (OSError, ValueError):
        raise RecalError(f"FILE_INVALID:{path.name}") from None


# --------------------------------------------------------------------------
# Export


def judged(item: Mapping[str, Any]) -> bool:
    verdicts = item.get("verdicts") or {}
    return any(verdicts.get(name) is not None for name in JUDGE_NAMES)


def pipeline_verdict(item: Mapping[str, Any]) -> bool | None:
    """The flow's automatic verdict: both judges' common verdict, or
    ``None`` when they disagree or either gave none."""
    values = [(item.get("verdicts") or {}).get(name) for name in JUDGE_NAMES]
    if any(v is None for v in values) or len(set(values)) != 1:
        return None
    return bool(values[0])


def redact(text: str) -> tuple[str, int]:
    return NAME_PATTERN.subn(NAME_REPLACEMENT, text)


def forbidden_terms(
    items: Sequence[Mapping[str, Any]], dataset_names: Sequence[str]
) -> list[str]:
    """Strings that must not appear anywhere in the pack."""
    terms = {"opspilot", "holmes", "glm-", "luna", "flashx", "gpt-5", "z.ai"}
    for item in items:
        terms.add(str(item["attempt_id"]).lower())
        terms.add(str(item["scenario_id"]).lower())
    for name in dataset_names:
        terms.add(name.lower())
    return sorted(terms)


def leak_hits(text: str, terms: Sequence[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t in low]


def build_pack(
    sources: Mapping[str, Mapping[str, Any]],
    questions: Mapping[tuple[str, str], Mapping[str, Any]],
    rulings: Mapping[str, str],
    published_pass: Mapping[str, bool],
    *,
    dataset_names: Sequence[str],
    seed: bytes,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Returns (pack, key, leak hits).

    ``sources`` maps a dataset label to its ``items.json`` document;
    ``questions`` maps (dataset label, scenario id) to ``{"question",
    "canary"}``; ``rulings`` and ``published_pass`` come from the published
    summary and are keyed by attempt id.
    """
    rows: list[dict[str, Any]] = []
    for label, doc in sources.items():
        if doc.get("schema") != ITEMS_SCHEMA:
            raise RecalError(f"ITEMS_SCHEMA_INVALID:{label}")
        voided = set(doc.get("voided") or ())
        for item in doc["attempts"]:
            if item["attempt_id"] in voided or not judged(item):
                continue
            q = questions.get((label, item["scenario_id"]))
            if q is None:
                raise RecalError(f"SCENARIO_UNKNOWN:{item['scenario_id']}")
            if item.get("report_text") is None:
                raise RecalError(f"NO_REPORT_TEXT:{item['attempt_id']}")
            rows.append({"dataset": label, "item": item, "q": q})
    ids = [r["item"]["attempt_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise RecalError("ATTEMPT_DUPLICATE")
    rows.sort(key=lambda r: (r["dataset"], r["item"]["attempt_id"]))
    random.Random(seed).shuffle(rows)
    width = max(3, len(str(len(rows))))
    pack_items: list[dict[str, Any]] = []
    key_items: dict[str, Any] = {}
    for n, row in enumerate(rows, start=1):
        item, q = row["item"], row["q"]
        blind = f"R{n:0{width}d}"
        output, redactions = redact(item["report_text"])
        entry: dict[str, Any] = {
            "id": blind,
            "question": q["question"],
            "expected_elements": list(item["expected_elements"]),
            "output": output,
        }
        if q.get("canary"):
            entry["injected_canary"] = q["canary"]
        pack_items.append(entry)
        aid = item["attempt_id"]
        verdicts = {
            name: (item.get("verdicts") or {}).get(name) for name in JUDGE_NAMES
        }
        key_items[blind] = {
            "attempt_id": aid,
            "dataset": row["dataset"],
            "system": item["system"],
            "scenario_id": item["scenario_id"],
            "slot": item["slot"],
            "verdicts": verdicts,
            "pipeline": pipeline_verdict(item),
            "user_ruling": rulings.get(aid),
            "published_pass": published_pass.get(aid),
            "invalid": any(v is None for v in verdicts.values())
            or any((item.get("errors") or {}).values()),
            "redactions": redactions,
        }
    pack = {"schema": PACK_SCHEMA, "count": len(pack_items), "items": pack_items}
    key = {
        "schema": IDENTITY_SCHEMA,
        "seed_hex": seed.hex(),
        "count": len(key_items),
        "items": key_items,
    }
    terms = forbidden_terms([r["item"] for r in rows], dataset_names)
    hits = leak_hits(json.dumps(pack, ensure_ascii=False), terms)
    return pack, key, hits


def label_template(pack: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": LABELS_SCHEMA,
        "labeller": "",
        "labels": {item["id"]: None for item in pack["items"]},
        "notes": {},
    }


def write_pack(
    out: Path, pack: Mapping[str, Any], key: Mapping[str, Any], instructions: bytes
) -> dict[str, Any]:
    files = {
        "pack/instructions.md": instructions,
        "pack/pack.json": _dump(pack),
        "pack/label-template.json": _dump(label_template(pack)),
        "private/identity-map.json": _dump(key),
    }
    for rel, data in files.items():
        _write_new(out / rel, data)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "count": pack["count"],
        "files": {rel: sha256_bytes(data) for rel, data in files.items()},
    }
    _write_new(out / "manifest.json", _dump(manifest))
    return manifest


def load_questions(
    dataset_roots: Mapping[str, Path],
) -> dict[tuple[str, str], dict[str, Any]]:
    from opspilot.evaluation.binary_judge import FAULT_QUESTION, NORMAL_QUESTION
    from opspilot.evaluation.model import load_scenarios_v2

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for label, root in dataset_roots.items():
        for _spec, answer in load_scenarios_v2(root):
            question = (
                FAULT_QUESTION if answer.signature is not None else NORMAL_QUESTION
            )
            canary = (
                answer.expected_outcome.canary
                if answer.category == "prompt_injection"
                else None
            )
            out[(label, answer.scenario_id)] = {
                "question": question.strip(),
                "canary": canary,
            }
    return out


def published_maps(
    summary: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, bool]]:
    rulings = {
        str(d["attempt_id"]): str(d["ruling"])
        for d in (summary.get("disagreements") or {}).get("items") or []
    }
    passed = {
        str(a["attempt_id"]): bool(a["pass"])
        for row in summary.get("per_scenario") or []
        for a in row.get("attempts") or []
    }
    return rulings, passed


def _pairs(values: Sequence[str], flag: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values:
        label, sep, path = value.partition("=")
        if not sep or not label or not path or label in out:
            raise RecalError(f"ARG_INVALID:{flag}")
        out[label] = Path(path)
    return out


def run_export(args: argparse.Namespace) -> dict[str, Any]:
    items = _pairs(args.items, "--items")
    roots = _pairs(args.dataset, "--dataset")
    if set(items) != set(roots):
        raise RecalError("DATASET_LABELS_DIFFER")
    sources = {label: _read_json(path) for label, path in items.items()}
    summary = _read_json(Path(args.published))
    rulings, passed = published_maps(summary)
    # dataset directory names and the holdout family name; the bare labels
    # ("dev") are ordinary words in report text
    names = [root.name for root in roots.values()] + ["holdout"]
    pack, key, hits = build_pack(
        sources,
        load_questions(roots),
        rulings,
        passed,
        dataset_names=names,
        seed=secrets.token_bytes(32),
    )
    instructions = Path(args.instructions).read_bytes()
    terms = forbidden_terms(
        [i for doc in sources.values() for i in doc["attempts"]], names
    )
    hits += leak_hits(instructions.decode("utf-8"), terms)
    hits += leak_hits(json.dumps(label_template(pack)), terms)
    if hits:
        raise RecalError(f"PACK_LEAK:{len(hits)}")
    key["sources_sha256"] = {
        label: sha256_bytes(path.read_bytes()) for label, path in items.items()
    }
    manifest = write_pack(Path(args.out), pack, key, instructions)
    return {"count": manifest["count"], "files": manifest["files"], "leak_hits": 0}


# --------------------------------------------------------------------------
# Freeze


def load_freeze(path: Path, pack_dir: Path) -> dict[str, Any]:
    """The frozen thresholds, after checking the pack is the frozen one."""
    freeze = _read_json(path)
    if freeze.get("schema") != FREEZE_SCHEMA:
        raise RecalError("FREEZE_SCHEMA_INVALID")
    for rel, digest in (freeze.get("pack_files") or {}).items():
        target = pack_dir / rel
        if not target.is_file() or sha256_bytes(target.read_bytes()) != digest:
            raise RecalError(f"PACK_NOT_FROZEN:{rel}")
    if not freeze.get("pack_files"):
        raise RecalError("FREEZE_PACK_FILES_MISSING")
    return freeze


# --------------------------------------------------------------------------
# Labels and adjudication


def validate_labels(
    doc: Any, ids: Sequence[str]
) -> tuple[str, dict[str, str], list[str]]:
    """(labeller, valid labels, ids whose label is missing or invalid)."""
    if not isinstance(doc, Mapping) or doc.get("schema") != LABELS_SCHEMA:
        raise RecalError("LABELS_SCHEMA_INVALID")
    labeller = doc.get("labeller")
    if not isinstance(labeller, str) or not labeller.strip():
        raise RecalError("LABELLER_MISSING")
    labels = doc.get("labels")
    if not isinstance(labels, Mapping):
        raise RecalError("LABELS_INVALID")
    unknown = set(labels) - set(ids)
    if unknown:
        raise RecalError(f"LABEL_ID_UNKNOWN:{len(unknown)}")
    valid = {i: labels[i] for i in ids if labels.get(i) in LABEL_VALUES}
    invalid = [i for i in ids if i not in valid]
    return labeller.strip(), valid, invalid


def merge_labels(key: Mapping[str, Any], docs: Sequence[Any]) -> dict[str, Any]:
    if len(docs) != 2:
        raise RecalError("TWO_LABEL_FILES_REQUIRED")
    ids = sorted(key["items"])
    parsed = [validate_labels(doc, ids) for doc in docs]
    if parsed[0][0] == parsed[1][0]:
        raise RecalError("LABELLERS_NOT_DISTINCT")
    invalid = {p[0]: p[2] for p in parsed if p[2]}
    if invalid:
        # labels are not merged until every item has a valid label from both
        return {"schema": MERGED_SCHEMA, "complete": False, "invalid": invalid}
    a, b = parsed[0][1], parsed[1][1]
    agreed = {i: a[i] for i in ids if a[i] == b[i]}
    disagreed = [i for i in ids if a[i] != b[i]]
    ruled = {i for i in ids if key["items"][i].get("user_ruling") is not None}
    return {
        "schema": MERGED_SCHEMA,
        "complete": True,
        "labellers": [parsed[0][0], parsed[1][0]],
        "labels": {parsed[0][0]: a, parsed[1][0]: b},
        "agreed": agreed,
        "disagreed": disagreed,
        # the 9 already ruled keep the user's ruling (Q3) and are not re-asked
        "to_adjudicate": [i for i in disagreed if i not in ruled],
        "labeller_agreement": {"agreed": len(agreed), "n": len(ids)},
    }


def adjudication_brief(
    pack: Mapping[str, Any], ids: Sequence[str], instructions: str
) -> str:
    """The user's one-time ruling list: report, question and expected
    elements only; no judge verdicts, no labeller labels."""
    by_id = {item["id"]: item for item in pack["items"]}
    lines = [
        "# 盲标分歧裁决简报",
        "",
        f"共 {len(ids)} 份。两位盲标者对这些报告给出的结论不同。"
        "这里不附裁判与盲标者的意见。请对每份写 pass 或 fail，"
        "填进同目录的 adjudication.json。",
        "",
        "## 盲标说明（与盲标者看到的相同）",
        "",
        instructions.strip(),
        "",
    ]
    for i in ids:
        item = by_id[i]
        lines += [
            f"## {i}",
            "",
            f"**QUESTION**: {item['question']}",
            "",
            "**EXPECTED ELEMENTS**",
            "",
        ]
        lines += [f"- {e}" for e in item["expected_elements"]]
        if item.get("injected_canary"):
            lines += ["", f"**INJECTED CANARY**: `{item['injected_canary']}`"]
        lines += ["", "**OUTPUT**", "", "````text", item["output"], "````", ""]
    return "\n".join(lines) + "\n"


def adjudication_template(ids: Sequence[str]) -> dict[str, Any]:
    return {"schema": ADJUDICATION_SCHEMA, "rulings": {i: None for i in ids}}


def run_labels(args: argparse.Namespace) -> dict[str, Any]:
    pack_dir = Path(args.pack)
    load_freeze(Path(args.freeze), pack_dir)
    key = _read_json(pack_dir / "private/identity-map.json")
    pack = _read_json(pack_dir / "pack/pack.json")
    docs = [_read_json(Path(p)) for p in args.label]
    merged = merge_labels(key, docs)
    out = Path(args.out)
    _write_new(out / "merged.json", _dump(merged))
    if not merged["complete"]:
        return {
            "complete": False,
            "invalid": {k: len(v) for k, v in merged["invalid"].items()},
        }
    ids = merged["to_adjudicate"]
    instructions = (pack_dir / "pack/instructions.md").read_text(encoding="utf-8")
    _write_new(
        out / "adjudication-brief.md",
        adjudication_brief(pack, ids, instructions).encode(),
    )
    _write_new(out / "adjudication.json", _dump(adjudication_template(ids)))
    return {
        "complete": True,
        "labeller_agreement": merged["labeller_agreement"],
        "to_adjudicate": len(ids),
    }


# --------------------------------------------------------------------------
# Compare


def gold_labels(
    key: Mapping[str, Any], merged: Mapping[str, Any], adjudication: Any
) -> dict[str, dict[str, Any]]:
    """Final label per blind id with its source: ``blind`` (both labellers),
    ``user`` (adjudicated disagreement) or ``prior_ruling`` (Q3, non-blind)."""
    if not merged.get("complete"):
        raise RecalError("LABELS_INCOMPLETE")
    if (
        not isinstance(adjudication, Mapping)
        or adjudication.get("schema") != ADJUDICATION_SCHEMA
    ):
        raise RecalError("ADJUDICATION_SCHEMA_INVALID")
    rulings = adjudication.get("rulings") or {}
    expected = set(merged["to_adjudicate"])
    if set(rulings) != expected:
        raise RecalError("ADJUDICATION_IDS_DIFFER")
    missing = [i for i in expected if rulings[i] not in LABEL_VALUES]
    if missing:
        raise RecalError(f"ADJUDICATION_INCOMPLETE:{len(missing)}")
    gold: dict[str, dict[str, Any]] = {}
    for i, entry in key["items"].items():
        prior = entry.get("user_ruling")
        if prior is not None:
            gold[i] = {"label": prior, "source": "prior_ruling"}
        elif i in merged["agreed"]:
            gold[i] = {"label": merged["agreed"][i], "source": "blind"}
        elif i in expected:
            gold[i] = {"label": rulings[i], "source": "user"}
        else:
            raise RecalError(f"GOLD_MISSING:{i}")
    return gold


def compare(
    key: Mapping[str, Any],
    gold: Mapping[str, Mapping[str, Any]],
    threshold: Mapping[str, Any],
    published: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    items = key["items"]
    flow = [
        i
        for i, e in items.items()
        if e["pipeline"] is not None and e.get("user_ruling") is None
    ]
    if len(flow) != int(threshold["denominator"]):
        raise RecalError(f"DENOMINATOR_DIFFERS:{len(flow)}")
    agree = [i for i in flow if (gold[i]["label"] == "pass") == items[i]["pipeline"]]
    false_pass = [
        i for i in flow if items[i]["pipeline"] and gold[i]["label"] == "fail"
    ]
    false_fail = [
        i for i in flow if not items[i]["pipeline"] and gold[i]["label"] == "pass"
    ]
    invalid = [i for i in flow if items[i].get("invalid")]
    achieved = (
        len(agree) >= int(threshold["min_agree"])
        and len(false_pass) <= int(threshold["max_false_pass"])
        and len(invalid) <= int(threshold["max_invalid"])
    )

    def ref(i: str) -> dict[str, Any]:
        e = items[i]
        return {
            k: e[k] for k in ("attempt_id", "dataset", "system", "scenario_id", "slot")
        }

    per_judge: dict[str, Any] = {}
    for name in JUDGE_NAMES:
        rows = [i for i in items if items[i]["verdicts"].get(name) is not None]
        per_judge[name] = {
            "agree": sum(
                1
                for i in rows
                if (gold[i]["label"] == "pass") == items[i]["verdicts"][name]
            ),
            "false_pass": sum(
                1
                for i in rows
                if items[i]["verdicts"][name] and gold[i]["label"] == "fail"
            ),
            "n": len(rows),
        }
    overturned = [
        i
        for i, e in items.items()
        if e.get("published_pass") is not None
        and (gold[i]["label"] == "pass") != e["published_pass"]
    ]
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "threshold": dict(threshold),
        "flow": {
            "n": len(flow),
            "agree": len(agree),
            "false_pass": len(false_pass),
            "false_fail": len(false_fail),
            "invalid": len(invalid),
            "achieved": achieved,
            "false_pass_items": [ref(i) for i in sorted(false_pass)],
            "false_fail_items": [ref(i) for i in sorted(false_fail)],
        },
        "reference": {
            "per_judge": per_judge,
            "gold_sources": {
                s: sum(1 for g in gold.values() if g["source"] == s)
                for s in ("blind", "user", "prior_ruling")
            },
            "prior_rulings_non_blind": [
                ref(i) for i in sorted(items) if items[i].get("user_ruling") is not None
            ],
        },
        "published_overturned": [
            ref(i)
            | {"published_pass": items[i]["published_pass"], "gold": gold[i]["label"]}
            for i in sorted(overturned)
        ],
    }
    if published is not None:
        result["recomputed"] = recompute(published, key, gold)
    return result


def recompute(
    published: Mapping[str, Any],
    key: Mapping[str, Any],
    gold: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Per dataset and system, single-attempt passes with gold labels in
    place of the published verdicts of judged attempts (Q7 correction line).
    Unjudged attempts keep their published result."""
    by_attempt = {
        e["attempt_id"]: gold[i]["label"] == "pass" for i, e in key["items"].items()
    }
    out: dict[str, dict[str, Any]] = {}
    for row in published.get("per_scenario") or []:
        cell = out.setdefault(row["dataset"], {}).setdefault(
            row["system"], {"published": 0, "recomputed": 0, "n": 0}
        )
        for a in row.get("attempts") or []:
            cell["n"] += 1
            cell["published"] += bool(a["pass"])
            cell["recomputed"] += by_attempt.get(a["attempt_id"], bool(a["pass"]))
    return out


def run_compare(args: argparse.Namespace) -> dict[str, Any]:
    pack_dir = Path(args.pack)
    freeze = load_freeze(Path(args.freeze), pack_dir)
    key = _read_json(pack_dir / "private/identity-map.json")
    merged = _read_json(Path(args.merged))
    gold = gold_labels(key, merged, _read_json(Path(args.adjudication)))
    published = _read_json(Path(args.published)) if args.published else None
    result = compare(key, gold, freeze["threshold"], published)
    result["labeller_agreement"] = merged["labeller_agreement"]
    result["freeze_sha256"] = sha256_bytes(Path(args.freeze).read_bytes())
    _write_new(Path(args.out), _dump(result))
    return {"achieved": result["flow"]["achieved"], "out": args.out}


# --------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval_judge_recal")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export")
    e.add_argument("--items", action="append", required=True)
    e.add_argument("--dataset", action="append", required=True)
    e.add_argument("--published", required=True)
    e.add_argument("--instructions", required=True)
    e.add_argument("--out", required=True)
    lab = sub.add_parser("labels")
    lab.add_argument("--pack", required=True)
    lab.add_argument("--freeze", required=True)
    lab.add_argument("--label", action="append", required=True)
    lab.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--pack", required=True)
    c.add_argument("--freeze", required=True)
    c.add_argument("--merged", required=True)
    c.add_argument("--adjudication", required=True)
    c.add_argument("--published")
    c.add_argument("--out", required=True)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    run = {"export": run_export, "labels": run_labels, "compare": run_compare}[
        args.command
    ]
    try:
        result = run(args)
    except RecalError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(
            json.dumps({"error": f"REFUSE_OVERWRITE:{Path(exc.filename).name}"}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
