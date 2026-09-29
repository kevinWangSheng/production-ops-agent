"""Deterministic numeric-provenance check of a report against its cited views.

For every claim, each number written in ``text`` must be findable among the
numbers of the views the claim cites (any field of the view: rows, span_groups,
metric values, query). Nothing is computed: a number that only exists as a sum,
a count of rows or a rounding beyond 3 significant digits shows up as "not
found", which is the false-positive side of the check and is measured in the
README (``provenance-scan.json``).

usage: python provenance.py <ledger.json> [report.txt|report.json]
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal, InvalidOperation

NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")
UUIDISH = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:-t\d+)?(?::[0-9a-f-]{36})?"
)
HEX = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{12,}\b")
TS = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?")


def numbers_of(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in NUM.finditer(text):
        try:
            out.add(Decimal(m.group(1).replace(",", "")))
        except InvalidOperation:
            pass
    return out


def strip_ids(text: str) -> str:
    text = UUIDISH.sub(" ", text)
    text = TS.sub(" ", text)
    return HEX.sub(" ", text)


def view_numbers(view: dict) -> set[Decimal]:
    # canonical JSON text of the whole view; ids and timestamps removed so their
    # digit runs do not count as numbers. Metric values are JSON strings.
    return numbers_of(strip_ids(json.dumps(view, ensure_ascii=False)))


def matches(x: Decimal, pool: set[Decimal]) -> bool:
    if x in pool:
        return True
    # x may be a rounding of a pool value (8.33 for 8.3333...): compare at x's
    # own decimal places.
    places = -x.as_tuple().exponent if x.as_tuple().exponent < 0 else 0
    if places == 0:
        return False
    q = Decimal(1).scaleb(-places)
    return any(
        abs(p - x) <= q / 2 + Decimal("1e-9")
        for p in pool
        if p != p.to_integral_value() or True
        if abs(p - x) < 1
    )


def significant(x: Decimal) -> bool:
    """Numbers worth checking: 4+ integer digits, or a decimal with 2+ places."""
    s = format(x, "f")
    integer, _, frac = s.partition(".")
    return len(integer.lstrip("0")) >= 4 or len(frac.rstrip("0")) >= 2


def check(report: dict, views_by_eid: dict[str, dict]) -> list[dict]:
    findings = []
    pools: dict[str, set[Decimal]] = {}
    for i, claim in enumerate(report.get("claims", [])):
        cited = claim.get("evidence_ids") or []
        pool: set[Decimal] = set()
        for eid in cited:
            if eid not in views_by_eid:
                continue
            pools.setdefault(eid, view_numbers(views_by_eid[eid]))
            pool |= pools[eid]
        text = strip_ids(claim.get("text", ""))
        for x in sorted(numbers_of(text)):
            if not significant(x):
                continue
            if not matches(x, pool):
                elsewhere = [
                    e.split(":")[0][:8] + e.split(":")[0][-3:]
                    for e, v in views_by_eid.items()
                    if e not in cited
                    and matches(x, pools.setdefault(e, view_numbers(v)))
                ]
                findings.append(
                    {
                        "claim": i,
                        "kind": claim.get("kind"),
                        "number": format(x, "f"),
                        "in_other_views": elsewhere,
                    }
                )
    return findings


def views_of(ledger: dict) -> dict[str, dict]:
    return {e["evidence_id"]: e["view"] for e in ledger["evidence"]}


if __name__ == "__main__":
    led = json.load(open(sys.argv[1]))
    rep = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else None
    print(
        json.dumps(
            check(
                rep["report_parsed"] if "report_parsed" in rep else rep, views_of(led)
            ),
            indent=1,
        )
    )
