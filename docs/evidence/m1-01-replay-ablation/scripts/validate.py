"""Deterministic acceptance check for every replayed sample.

usage: ABLATION_WORK=<work dir> .venv/bin/python validate.py [--out validation.json]

Applies exactly what ``InvestigationLoop._validated_report`` applies before a
report can be published: ``parse_report`` (finish reason, DSML, JSON, schema)
then ``unsupported_citations`` against the views the Run delivered (from the
rebuilt ``validation.json`` of that Run). Reports the reason code per sample.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from opspilot.investigation.reports import (
    DeliveredView,
    parse_report,
    unsupported_citations,
)


def delivered_views(validation: dict) -> list[DeliveredView]:
    return [
        DeliveredView(
            evidence_id=v["evidence_id"],
            target_ids=frozenset(v["target_ids"]),
            status=v["status"],
            time_scope_refs=frozenset(v["time_scope_refs"]),
            citable_as_fact=v["citable_as_fact"],
            incomplete=v["incomplete"],
            truncated=v["truncated"],
            inherited=v["inherited"],
        )
        for v in validation["delivered"]
    ]


def citation_details(report, validation: dict) -> list[str]:
    """Which binding rule(s) the citation check tripped on (diagnostic only;
    the verdict itself comes from ``unsupported_citations``)."""
    by = {v["evidence_id"]: v for v in validation["delivered"]}
    catalog = validation["target_catalog"]
    authorized = set(validation["authorized_targets"])
    policies = set(validation["time_policy_ids"])
    factlike = {"fact", "counter_evidence", "rejected_hypothesis"}
    found = set()
    for c in report.claims:
        if any(e not in by for e in c.evidence_ids):
            found.add("unknown_evidence_id")
        refs_ok = (
            all(r in catalog for r in c.target_refs)
            if catalog is not None
            else all(r in authorized for r in c.target_refs)
        )
        if not refs_ok:
            found.add("target_ref_not_authorized")
        if c.time_scope_ref is not None and c.time_scope_ref not in policies:
            found.add("unknown_time_scope_ref")
        if c.kind not in factlike:
            continue
        cited = [by[e] for e in c.evidence_ids if e in by]
        if any(v["status"] != "ok" or not v["citable_as_fact"] for v in cited):
            found.add("fact_cites_non_ok_view")
        if any(c.time_scope_ref not in v["time_scope_refs"] for v in cited):
            found.add("time_scope_not_on_view")
        observed = set()
        for v in cited:
            observed.update(v["target_ids"])
        if any(r not in observed for r in c.target_refs):
            found.add("target_ref_not_observed_by_view")
    return sorted(found)


def check(content: str, finish_reason: str, validation: dict) -> dict:
    report, reason = parse_report(content, finish_reason=finish_reason)
    if report is None:
        return {"accepted": False, "reason": reason, "stage": "parse"}
    if unsupported_citations(
        report,
        views=delivered_views(validation),
        authorized_targets=frozenset(validation["authorized_targets"]),
        time_policy_ids=validation["time_policy_ids"],
        target_catalog=validation["target_catalog"],
    ):
        return {
            "accepted": False,
            "reason": "REPORT_INVALID",
            "stage": "citations",
            "details": citation_details(report, validation),
            "assessment_status": report.assessment_status,
            "conclusion": report.conclusion,
            "claims": len(report.claims),
        }
    return {
        "accepted": True,
        "reason": "",
        "stage": "",
        "assessment_status": report.assessment_status,
        "conclusion": report.conclusion,
        "claims": len(report.claims),
        "gaps": len(report.gaps),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    work = Path(os.environ["ABLATION_WORK"])
    results = {}
    for sample in sorted((work / "samples").iterdir()):
        meta = json.load(open(sample / "meta.json"))
        validation = json.load(
            open(work / "requests" / meta["case"] / "validation.json")
        )
        if "error" in meta:
            results[sample.name] = {
                "accepted": False,
                "reason": meta["error"],
                "stage": "http",
            }
            continue
        content = (sample / "report.txt").read_text()
        results[sample.name] = check(content, meta["finish_reason"], validation)
    for name, r in results.items():
        print(
            name,
            "ACCEPT"
            if r["accepted"]
            else f"REJECT {r['reason']} ({r['stage']}) {r.get('details', '')}",
            r.get("assessment_status", ""),
            r.get("conclusion", ""),
            r.get("claims", ""),
        )
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
