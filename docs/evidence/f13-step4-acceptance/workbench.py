"""Lab reviewer actions for the F13 step 4 acceptance runs (not product code).

    workbench.py ask --like-incident <uuid> --question <text> --owner-dsn DSN --web URL --user-file F --out out.json
    workbench.py revoke --entry <uuid> --reason <text> --owner-dsn DSN --web URL --user-file F --out out.json

``ask`` submits the incident form a human would (``POST /intake/ui``) for
the same target as ``--like-incident`` (the knowledge's source incident,
whose ``resource_uid`` is read first); the web process retrieves knowledge
for the new Run (Run 3, healthy system).
``revoke`` revokes the entry's active revision through the knowledge page's
form (``POST /knowledge/<entry>/revoke``), reading the expected generation
and revision with the owner DSN in a read-only session first (Run 2). The
Basic Auth user is read from ``--user-file`` (``name:password``) and never
printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx2
import psycopg
from psycopg.rows import dict_row


def _read(dsn: str, sql: str, params: tuple) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return [
            {
                k: (v if isinstance(v, (int, float, bool, type(None))) else str(v))
                for k, v in row.items()
            }
            for row in conn.execute(sql, params).fetchall()
        ]


def _client(web: str, user_file: Path) -> httpx2.Client:
    user, password = user_file.read_text().strip().split(":", 1)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    del password
    base = web.rstrip("/")
    return httpx2.Client(
        base_url=base,
        headers={
            "authorization": f"Basic {token}",
            "origin": base,
            "accept": "application/json",
        },
        timeout=30.0,
    )


def _body(response: httpx2.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def ask(args: argparse.Namespace) -> dict:
    (target,) = _read(
        args.owner_dsn,
        "SELECT t.resource_uid FROM opspilot_incidents i"
        " JOIN opspilot_targets t ON t.target_id=i.target_id WHERE i.incident_id=%s",
        (args.like_incident,),
    )
    response = _client(args.web, args.user_file).post(
        "/intake/ui",
        data={
            "target_id": target["resource_uid"],
            "question": args.question,
            "idempotency_key": f"f13-acc-{uuid4().hex}",
        },
    )
    return {
        "target_id": target["resource_uid"],
        "question": args.question,
        "http_status": response.status_code,
        "http_body": _body(response),
    }


def revoke(args: argparse.Namespace) -> dict:
    (head,) = _read(
        args.owner_dsn,
        "SELECT generation FROM opspilot_knowledge_entries WHERE entry_id=%s",
        (args.entry,),
    )
    states = "SELECT revision, state FROM opspilot_knowledge_revision_states WHERE entry_id=%s ORDER BY revision"
    before = _read(args.owner_dsn, states, (args.entry,))
    active = [row["revision"] for row in before if row["state"] == "active"]
    if len(active) != 1:
        return {"before": before, "error": "NO_SINGLE_ACTIVE_REVISION"}
    response = _client(args.web, args.user_file).post(
        f"/knowledge/{args.entry}/revoke",
        data={
            "revision": str(active[0]),
            "reason": args.reason,
            "idempotency_key": f"f13-acc-{uuid4().hex}",
            "expected_generation": str(head["generation"]),
        },
    )
    return {
        "revoked_revision": active[0],
        "expected_generation": head["generation"],
        "before": before,
        "http_status": response.status_code,
        "http_body": _body(response),
        "after": _read(args.owner_dsn, states, (args.entry,)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ask", "revoke"):
        command = sub.add_parser(name)
        command.add_argument("--web", required=True)
        command.add_argument("--user-file", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--owner-dsn", required=True)
    sub.choices["ask"].add_argument("--like-incident", required=True)
    sub.choices["ask"].add_argument("--question", required=True)
    sub.choices["revoke"].add_argument("--entry", required=True)
    sub.choices["revoke"].add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    result = ask(args) if args.command == "ask" else revoke(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result))
    return 0 if result.get("http_status") in (200, 201) else 1


if __name__ == "__main__":
    sys.exit(main())
