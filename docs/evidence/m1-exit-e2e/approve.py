"""Approve the incident's postmortem draft through the workbench (not product code).

The M1 exit run's last segment: a lab Basic Auth reviewer opens the version
page and submits the page's approve action, as a human reviewer would. The
script then reads back, with the owner DSN in a read-only session, the version
state, the review audit and the published knowledge revision. The password is
read from ``--user-file`` (``name:password``) and never printed.

    approve.py --incident <uuid> --owner-dsn ... --web http://127.0.0.1:8080 \\
        --user-file <name:password> --out <approve.json>
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import psycopg
from psycopg.rows import dict_row


def _read(dsn: str, sql: str, params: tuple) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return [
            {
                k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
                for k, v in row.items()
            }
            for row in conn.execute(sql, params).fetchall()
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident", type=UUID, required=True)
    parser.add_argument("--owner-dsn", required=True)
    parser.add_argument("--web", required=True)
    parser.add_argument("--user-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    user, password = args.user_file.read_text().strip().split(":", 1)
    base = args.web.rstrip("/")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    del password
    client = httpx2.Client(
        base_url=base,
        headers={"authorization": f"Basic {token}", "origin": base},
        timeout=30.0,
    )

    (pm,) = _read(
        args.owner_dsn,
        "SELECT postmortem_id, generation, latest_version FROM opspilot_postmortems"
        " WHERE incident_id=%s",
        (args.incident,),
    )
    before = _read(
        args.owner_dsn,
        "SELECT version, state, content_sha256 FROM opspilot_postmortem_versions"
        " WHERE postmortem_id=%s AND version=%s",
        (pm["postmortem_id"], pm["latest_version"]),
    )
    page = client.get(
        f"/postmortems/{pm['postmortem_id']}/versions/{pm['latest_version']}",
        headers={"accept": "text/html"},
    )
    response = client.post(
        f"/postmortems/{pm['postmortem_id']}/versions/{pm['latest_version']}/review",
        data={
            "action": "approve",
            "idempotency_key": f"e2e-{uuid4().hex}",
            "expected_generation": str(pm["generation"]),
        },
        headers={"accept": "application/json"},
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    after = _read(
        args.owner_dsn,
        "SELECT version, state, content_sha256 FROM opspilot_postmortem_versions"
        " WHERE postmortem_id=%s ORDER BY version",
        (pm["postmortem_id"],),
    )
    knowledge = _read(
        args.owner_dsn,
        "SELECT entry_id, revision, name, tags, content_sha256, source_version,"
        " approved_by, approved_by_kind, approved_at"
        " FROM opspilot_knowledge_revisions WHERE source_postmortem_id=%s",
        (pm["postmortem_id"],),
    )
    result = {
        "at": _read(args.owner_dsn, "SELECT now()::text AS t", ())[0]["t"],
        "postmortem_id": pm["postmortem_id"],
        "version": pm["latest_version"],
        "expected_generation": pm["generation"],
        "before": before,
        "page_status": page.status_code,
        "page_has_approve_form": "form-approve" in page.text,
        "http_status": response.status_code,
        "http_body": body,
        "after": after,
        "knowledge_revisions": knowledge,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: result[k] for k in ("page_status", "http_status", "after")}))
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
