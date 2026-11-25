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

#780 (780-10): 162's ``global_instructions.json`` is imported the same way as
the target's standing instructions and revoked with the runbooks;
``active_standing_instructions`` and ``run_standing_instructions`` are its
two checks (residue, and the fact the Run's input recorded).

No runbook or instruction text is printed or written to evidence;
``fixture_rows`` and ``standing_fixture_row`` keep names, kinds, references
and byte sizes only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from scripts.holmes_evals.import_skills import (
    Account,
    fits_standing_limit,
    import_global_instructions,
    import_question,
    read_fixtures,
    read_global_instructions,
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


def import_standing_instructions(
    web_url: str, account: UiAccount, question_dir: Path, target: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Submit and approve the global instructions; (manifest rows, refusals)."""
    return import_global_instructions(
        workbench_account(web_url, account), question_dir, target
    )


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


def standing_fixture_row(question_dir: Path) -> dict[str, Any] | None:
    """What the summary records of the global instructions (no content)."""
    fixture = read_global_instructions(question_dir)
    if fixture is None:
        return None
    return {
        "name": fixture.name,
        "source_ref": fixture.source_ref,
        "content_bytes": len(fixture.content.encode("utf-8")),
        "fits": fits_standing_limit(fixture),
    }


def active_runbooks(owner_dsn: str, target: str) -> list[str]:
    """Active runbook names (``kind = 'runbook'``; #780 standing instructions
    are checked by ``active_standing_instructions``)."""
    with psycopg.connect(owner_dsn, row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT r.name FROM opspilot_runbook_revisions r "
            "JOIN opspilot_runbook_revision_states s USING (entry_id, revision) "
            "WHERE r.resource_uid=%s AND r.kind='runbook' AND s.state='active' "
            "ORDER BY r.name",
            (target,),
        ).fetchall()
    return [str(row["name"]) for row in rows]


def active_standing_instructions(owner_dsn: str, target: str) -> list[str]:
    """Entry ids of the target's active standing-instructions revisions."""
    with psycopg.connect(owner_dsn, row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT r.entry_id::text AS entry_id FROM opspilot_runbook_revisions r "
            "JOIN opspilot_runbook_revision_states s USING (entry_id, revision) "
            "WHERE r.resource_uid=%s AND r.kind='standing_instructions' "
            "AND s.state='active' ORDER BY r.entry_id",
            (target,),
        ).fetchall()
    return [str(row["entry_id"]) for row in rows]


def run_standing_instructions(owner_dsn: str, run_id: str) -> dict[str, Any] | None:
    """The Run input's ``standing_instructions`` fact's ``entry_id`` and
    ``revision`` (not its content), or None when the input has none."""
    with psycopg.connect(owner_dsn, row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "SELECT input -> 'scope_facts' -> 'standing_instructions' AS fact "
            "FROM opspilot_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()
    fact = None if row is None else row["fact"]
    if not isinstance(fact, Mapping):
        return None
    return {"entry_id": fact.get("entry_id"), "revision": fact.get("revision")}


def run_catalog(owner_dsn: str, run_id: str) -> dict[str, Any] | None:
    """The Run input's ``runbook_catalog`` fact as stored (``status``,
    ``omitted_for_size``, ``entries[].skill_id`` among its fields), or None."""
    with psycopg.connect(owner_dsn, row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "SELECT input -> 'scope_facts' -> 'runbook_catalog' AS fact "
            "FROM opspilot_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()
    fact = None if row is None else row["fact"]
    return dict(fact) if isinstance(fact, Mapping) else None


def catalog_summary(fact: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the summary keeps of the fact: status, names, omitted count."""
    if fact is None:
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


def catalog_matches(fact: Mapping[str, Any] | None, names: Sequence[str]) -> bool:
    summary = catalog_summary(fact)
    return (
        summary is not None
        and summary["status"] == "ok"
        and summary["omitted_for_size"] == 0
        and summary["skill_ids"] == sorted(names)
    )
