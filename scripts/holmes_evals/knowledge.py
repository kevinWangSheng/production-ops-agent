"""Knowledge fixtures of the C2 questions, per attempt (#666 PR-D, #566).

Interface: ``docs/tasks/2026-11-18-holmes-evals-666.md`` "PR-D 接口". Every
attempt of a C2 question imports and approves that question's own runbooks
through the workbench review flow (``scripts.holmes_evals.import_skills``,
source ``HolmesGPT 评测夹具``) before the worker starts, and revokes them
after the Run; the checks here are what fail closed:

* ``active_runbooks`` -- the target's active runbook names, read with the
  attempt's PG owner connection (one read-only SELECT), before import, after
  import and after revocation;
* ``run_catalog`` -- the ``runbook_catalog`` fact the Run's input recorded
  (the list the model was shown).

No runbook text is printed or written to evidence; ``fixture_rows`` keeps
names, kinds, references and byte sizes only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from scripts.holmes_evals.import_skills import (
    Account,
    import_question,
    read_fixtures,
    revoke_manifest,
)
from scripts.holmes_evals.intake import UiAccount

#: The import account's name prefix (audit: who reviewed).
IMPORT_ACCOUNT_PREFIX = "holmes-eval-import-"


def workbench_account(web_url: str, account: UiAccount) -> Account:
    base = web_url.rstrip("/")
    return Account(base, account.name, account.password, base)


def import_runbooks(
    web_url: str, account: UiAccount, question_dir: Path, target: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Submit and approve every fixture; (manifest rows, refusals)."""
    return import_question(workbench_account(web_url, account), question_dir, target)


def revoke_runbooks(
    web_url: str, account: UiAccount, rows: Sequence[Mapping[str, Any]]
) -> list[str]:
    return revoke_manifest(workbench_account(web_url, account), rows)


def fixture_rows(question_dir: Path) -> list[dict[str, Any]]:
    """What the summary records of the question's fixtures (no content)."""
    return [
        {
            "name": fixture.name,
            "kind": "skill_md" if "/SKILL.md#" in fixture.source_ref else "dal",
            "source_ref": fixture.source_ref,
            "content_bytes": len(fixture.content.encode("utf-8")),
        }
        for fixture in read_fixtures(question_dir)
    ]


def active_runbooks(owner_dsn: str, target: str) -> list[str]:
    with psycopg.connect(owner_dsn, row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT r.name FROM opspilot_runbook_revisions r "
            "JOIN opspilot_runbook_revision_states s USING (entry_id, revision) "
            "WHERE r.resource_uid=%s AND s.state='active' ORDER BY r.name",
            (target,),
        ).fetchall()
    return [str(row["name"]) for row in rows]


def run_catalog(owner_dsn: str, run_id: str) -> dict[str, Any] | None:
    """The Run input's ``runbook_catalog`` fact (status, names, omitted)."""
    with psycopg.connect(owner_dsn, row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "SELECT input -> 'scope_facts' -> 'runbook_catalog' AS fact "
            "FROM opspilot_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()
    fact = None if row is None else row["fact"]
    if not isinstance(fact, Mapping):
        return None
    entries = fact.get("entries")
    return {
        "status": fact.get("status"),
        "skill_ids": sorted(
            str(e.get("skill_id")) for e in entries if isinstance(e, Mapping)
        )
        if isinstance(entries, list)
        else [],
        "omitted_for_size": fact.get("omitted_for_size"),
    }


def catalog_matches(catalog: Mapping[str, Any] | None, names: Sequence[str]) -> bool:
    return (
        catalog is not None
        and catalog.get("status") == "ok"
        and catalog.get("omitted_for_size") == 0
        and list(catalog.get("skill_ids") or []) == sorted(names)
    )
