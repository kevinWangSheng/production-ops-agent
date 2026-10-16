"""Freeze one acceptance Run's knowledge use and check it (not product code).

    knowledge_use.py --incident <uuid> --scenario {consistent,changed,stale-contradicted}
        --entry <uuid> [--revision N] [--absent-revision N]
        --owner-dsn DSN --web URL --user-file F --out knowledge-use.json

Reads the incident's current Run with ``knowledge_use_records`` over a
read-only owner session (the same ``rebuild`` the incident page uses),
projects it with ``knowledge_use_outcome`` and fetches the incident page's
``#knowledge-hints`` section as the reviewer sees it. The scenario's checks
come from the task record's acceptance judgement
(``docs/tasks/2026-10-15-f13-step4-acceptance.md``); ``observed`` items are
recorded as they are and do not decide the result. Exit 0 only when every
check passed. The Basic Auth user is read from ``--user-file`` and never
printed.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import html
import json
import re
import sys
from pathlib import Path
from uuid import UUID

import httpx2

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from opspilot.acceptance import (  # noqa: E402
    IncidentScenario,
    KnowledgeUseOutcome,
    knowledge_use_outcome,
    knowledge_use_records,
)
from opspilot.persistence import DurableStore  # noqa: E402


def _page_section(web: str, user_file: Path, incident: UUID) -> tuple[int, str]:
    user, password = user_file.read_text().strip().split(":", 1)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    del password
    response = httpx2.get(
        f"{web.rstrip('/')}/incidents/{incident}",
        headers={"authorization": f"Basic {token}", "accept": "text/html"},
        timeout=30.0,
    )
    found = re.search(
        r'<section id="knowledge-hints">(.*?)</section>', response.text, re.S
    )
    return response.status_code, "" if found is None else found.group(1)


_ITEM = re.compile(
    r"r(\d+) · (\S+) · age (-?\d+) seconds · source incident"
    r" · truncated: (True|False) · check: (\S+)"
)


def _page_items(section: str) -> list[dict]:
    """Each ``<li>`` of the section as the fields the template prints: the
    entry id from its ``/knowledge/<id>`` link, then the text after the
    link (the name is skipped, so it cannot be mistaken for a field)."""
    items = []
    for raw in re.findall(r"<li>(.*?)</li>", section, re.S):
        found = re.match(r'\s*<a href="/knowledge/([^"]+)">.*?</a>(.*)$', raw, re.S)
        rest = (
            ""
            if found is None
            else " ".join(
                html.unescape(re.sub(r"<[^>]+>", " ", found.group(2))).split()
            )
        )
        fields = _ITEM.fullmatch(rest)
        items.append(
            {
                "text": rest,
                "fields": None
                if found is None or fields is None
                else [
                    found.group(1),
                    int(fields.group(1)),
                    fields.group(2),
                    int(fields.group(3)),
                    fields.group(4) == "True",
                    fields.group(5),
                ],
            }
        )
    return items


def _checks(
    args: argparse.Namespace, outcome: KnowledgeUseOutcome, items: list[dict]
) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    observed: dict = {}

    def check(name: str, passed: bool, detail: object = None) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    entry = str(args.entry)
    mine = [e for e in outcome.entries if e.entry_id == entry]
    check("status_ok", outcome.status == "ok", outcome.status)
    # The page shows exactly the projected entries, in order, with the same
    # revision, freshness, age and check result (U8: same data, not recomputed).
    check("page_entry_count", len(items) == len(outcome.entries), len(items))
    for index, (hint, item) in enumerate(zip(outcome.entries, items)):
        expected = [
            hint.entry_id,
            hint.revision,
            hint.freshness,
            hint.age_seconds,
            hint.truncated,
            hint.verdict,
        ]
        check(
            f"page_matches_projection[{index}]",
            item["fields"] == expected,
            {"page": item["fields"], "projection": expected},
        )
    if args.scenario == "consistent":
        check("entry_provided", len(mine) == 1, [e.entry_id for e in outcome.entries])
        if mine:
            check("entry_revision", mine[0].revision == args.revision, mine[0].revision)
            check("entry_fresh", mine[0].freshness == "fresh", mine[0].freshness)
            # Contract: recorded as it is (expected consistent with this Run's
            # evidence); not a pass condition of this Run.
            observed.update(
                verdict=mine[0].verdict,
                handled=mine[0].handled,
                claim_kinds=list(mine[0].claim_kinds),
            )
    elif args.scenario == "changed":
        present = {(e.entry_id, e.revision) for e in outcome.entries}
        check(
            "revoked_revision_absent",
            (entry, args.absent_revision) not in present,
            sorted(map(list, present)),
        )
        if args.revision is not None:
            check(
                "replacement_revision_present",
                (entry, args.revision) in present,
                sorted(map(list, present)),
            )
    else:
        check("entry_provided", len(mine) == 1, [e.entry_id for e in outcome.entries])
        if mine:
            hint = mine[0]
            check("entry_stale", hint.freshness == "stale", hint.freshness)
            check("verdict_contradicted", hint.verdict == "contradicted", hint.verdict)
            check("handled_with_run_evidence", hint.handled, hint.tagged_claims)
            check(
                "no_fact_on_knowledge", "fact" not in hint.claim_kinds, hint.claim_kinds
            )
            index = outcome.entries.index(hint)
            shown = items[index]["fields"] if index < len(items) else None
            check(
                "page_shows_stale_and_contradicted",
                shown is not None
                and shown[0] == entry
                and shown[2] == "stale"
                and shown[5] == "contradicted",
                shown,
            )
        check(
            "no_unprovided_tags", outcome.unprovided_tags == (), outcome.unprovided_tags
        )
    return checks, observed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident", type=UUID, required=True)
    parser.add_argument(
        "--scenario",
        choices=("consistent", "changed", "stale-contradicted"),
        required=True,
    )
    parser.add_argument("--entry", type=UUID, required=True)
    parser.add_argument("--revision", type=int)
    parser.add_argument("--absent-revision", type=int)
    parser.add_argument("--owner-dsn", required=True)
    parser.add_argument("--web", required=True)
    parser.add_argument("--user-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    # The scenario's core assertion needs its revision; never skip it.
    if args.scenario == "consistent" and args.revision is None:
        parser.error("--scenario consistent requires --revision")
    if args.scenario == "changed" and args.absent_revision is None:
        parser.error("--scenario changed requires --absent-revision")

    store = DurableStore(
        f"{args.owner_dsn} options='-c default_transaction_read_only=on'"
    )
    records = knowledge_use_records(store, args.incident)
    outcome = knowledge_use_outcome(
        IncidentScenario(
            scenario_id=f"F13:step4:{args.scenario}",
            feature_id="F13",
            acceptance_step="4",
            kind="live",
            subject_id=str(args.incident),
        ),
        records,
    )
    status, section = _page_section(args.web, args.user_file, args.incident)
    items = _page_items(section)
    checks, observed = _checks(args, outcome, items)
    passed = status == 200 and all(item["passed"] for item in checks)
    result = {
        "scenario": args.scenario,
        "incident_id": str(args.incident),
        "outcome": dataclasses.asdict(outcome),
        "page_status": status,
        "page_items": items,
        "checks": checks,
        "observed": observed,
        "passed": passed,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    )
    print(
        json.dumps(
            {
                "passed": passed,
                "failed": [c["name"] for c in checks if not c["passed"]],
                "observed": observed,
            }
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
