"""Import one HolmesGPT question's knowledge fixtures as reviewed runbooks (C2b, #566).

R8-14 as revised by r11 C2-U1=a; interface
``docs/tasks/2026-11-19-tool-expansion-c2.md`` section 7 and "C2b 签名级细节".

The fixtures go through the workbench's own review flow, as a person would:
``POST /runbooks`` (submit) and ``POST /runbooks/submissions/{id}/review``
(approve), form-encoded, Basic credentials, the web's ``Origin`` and
``Accept: application/json``. Nothing is written to a table directly. The
source label is fixed to ``HolmesGPT 评测夹具``; the reference names the
upstream commit, the file and its sha256.

Per question (C2-12): ``import`` before the question's trials, ``revoke``
after, so each question sees only its own runbooks, as upstream loads one
catalog per question.

Read, in the question directory only: ``*/SKILL.md`` (frontmatter ``name``
and ``description``, the body as content), or ``runbook_catalog.json`` with
``runbook_content_<id>.json`` (``title`` as name, ``symptom`` as
description, ``instruction`` as content). ``test_case.yaml`` and every other
file (``global_instructions.json`` included) are never opened. Runbook text
is never printed.

Usage::

    python -m scripts.holmes_evals.import_skills import \
        --question-dir <dir> --target <resource_uid> --manifest <path>
    python -m scripts.holmes_evals.import_skills revoke --manifest <path>

Environment: ``OPSPILOT_WEB_URL``, ``OPSPILOT_IMPORT_USER``,
``OPSPILOT_IMPORT_PASSWORD`` and (optional; defaults to the URL's origin)
``OPSPILOT_WEB_ORIGIN``. Exit 0 when every request succeeded, 1 when any was
refused (its code is printed), 2 on a usage error.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

SOURCE_LABEL = "HolmesGPT 评测夹具"
UPSTREAM = "holmesgpt@a0009195"
FIXTURES_ROOT = "tests/llm/fixtures/test_ask_holmes"
REVOKE_REASON = "评测题切换"
TIMEOUT_S = 30
DESCRIPTION_MAX = 300
_SEPARATORS = re.compile(r"[^a-z0-9]+")
_CONTENT_FILE = re.compile(r"runbook_content_[A-Za-z0-9-]+\.json")


@dataclass(frozen=True)
class Fixture:
    name: str
    description: str
    content: str = field(repr=False)
    source_ref: str


@dataclass(frozen=True)
class Account:
    web_url: str
    user: str
    password: str = field(repr=False)
    origin: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Account:
        url = env.get("OPSPILOT_WEB_URL", "").rstrip("/")
        user = env.get("OPSPILOT_IMPORT_USER", "")
        password = env.get("OPSPILOT_IMPORT_PASSWORD", "")
        if not url or not user or not password:
            raise SystemExit(
                "OPSPILOT_WEB_URL, OPSPILOT_IMPORT_USER and "
                "OPSPILOT_IMPORT_PASSWORD are required"
            )
        parts = urllib.parse.urlsplit(url)
        origin = env.get("OPSPILOT_WEB_ORIGIN") or f"{parts.scheme}://{parts.netloc}"
        return cls(url, user, password, origin)

    def basic(self) -> str:
        pair = f"{self.user}:{self.password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")


# -- reading the fixtures ------------------------------------------------------


def runbook_name(raw: str) -> str:
    """A runbook name the workbench accepts: lowercase, every run of other
    characters one hyphen, at most 100 characters."""
    return _SEPARATORS.sub("-", raw.lower()).strip("-")[:100].strip("-")


def _ref(question_dir: Path, path: Path, data: bytes) -> str:
    relative = path.relative_to(question_dir.parent).as_posix()
    digest = hashlib.sha256(data).hexdigest()
    return f"{UPSTREAM}:{FIXTURES_ROOT}/{relative}#sha256={digest}"


def _description(raw: object, fallback: str) -> str:
    text = " ".join(str(raw).split()) if raw else fallback
    return text[:DESCRIPTION_MAX]


def read_fixtures(question_dir: Path) -> list[Fixture]:
    """The question's runbooks, sorted by name; only the allowed files."""
    fixtures = [*_skill_files(question_dir), *_dal_runbooks(question_dir)]
    return sorted(fixtures, key=lambda fixture: fixture.name)


def _skill_files(question_dir: Path) -> list[Fixture]:
    found = []
    for path in sorted(question_dir.glob("*/SKILL.md")):
        data = path.read_bytes()
        text = data.decode("utf-8")
        meta: dict[str, Any] = {}
        body = text
        if text.startswith("---"):
            _, header, body = text.split("---", 2)
            loaded = yaml.safe_load(header)
            meta = loaded if isinstance(loaded, dict) else {}
        name = runbook_name(str(meta.get("name") or path.parent.name))
        found.append(
            Fixture(
                name=name,
                description=_description(meta.get("description"), name),
                content=body.strip() + "\n",
                source_ref=_ref(question_dir, path, data),
            )
        )
    return found


def _dal_runbooks(question_dir: Path) -> list[Fixture]:
    catalog = question_dir / "runbook_catalog.json"
    if not catalog.is_file():
        return []
    entries = json.loads(catalog.read_text("utf-8"))
    found = []
    for entry in entries if isinstance(entries, list) else []:
        runbook_id = str(entry.get("id", ""))
        filename = f"runbook_content_{runbook_id}.json"
        if not _CONTENT_FILE.fullmatch(filename):
            continue
        path = question_dir / filename
        if not path.is_file():
            continue
        data = path.read_bytes()
        content = json.loads(data.decode("utf-8"))
        name = runbook_name(str(content.get("title") or entry.get("title") or ""))
        if not name:
            continue
        found.append(
            Fixture(
                name=name,
                description=_description(
                    content.get("symptom") or entry.get("symptom"), name
                ),
                content=str(content.get("instruction") or "").strip() + "\n",
                source_ref=_ref(question_dir, path, data),
            )
        )
    return found


# -- the workbench -------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict[str, Any]

    @property
    def code(self) -> str | None:
        value = self.body.get("code")
        return value if isinstance(value, str) else None


def post(
    account: Account,
    path: str,
    fields: Mapping[str, object],
    *,
    opener: urllib.request.OpenerDirector | None = None,
) -> Reply:
    body = urllib.parse.urlencode(
        {key: str(value) for key, value in fields.items() if value is not None}
    ).encode("utf-8")
    request = urllib.request.Request(
        account.web_url + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": account.origin,
            "Authorization": account.basic(),
        },
    )
    try:
        with (opener or _OPENER).open(request, timeout=TIMEOUT_S) as response:
            status, raw = int(response.status), response.read()
    except urllib.error.HTTPError as error:
        status, raw = int(error.code), error.read()
    try:
        reply = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, ValueError):
        reply = None
    return Reply(status, reply if isinstance(reply, dict) else {})


def import_question(
    account: Account,
    question_dir: Path,
    target: str,
    *,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Submit and approve every fixture; returns (manifest rows, refusals)."""
    rows: list[dict[str, Any]] = []
    refusals: list[str] = []
    for fixture in read_fixtures(question_dir):
        submitted = post(
            account,
            "/runbooks",
            {
                "idempotency_key": f"holmes-import-{uuid.uuid4().hex}",
                "resource_uid": target,
                "name": fixture.name,
                "description": fixture.description,
                "content": fixture.content,
                "source_label": SOURCE_LABEL,
                "source_ref": fixture.source_ref,
            },
            opener=opener,
        )
        submission_id = submitted.body.get("submission_id")
        if submitted.status != 200 or not isinstance(submission_id, str):
            refusals.append(
                f"submit {fixture.name}: {submitted.code or submitted.status}"
            )
            continue
        approved = post(
            account,
            f"/runbooks/submissions/{submission_id}/review",
            {
                "idempotency_key": f"holmes-approve-{uuid.uuid4().hex}",
                "action": "approve",
                "expected_generation": 1,
            },
            opener=opener,
        )
        if approved.status != 200:
            refusals.append(
                f"approve {fixture.name}: {approved.code or approved.status}"
            )
            continue
        rows.append(
            {
                "entry_id": approved.body.get("entry_id"),
                "revision": approved.body.get("revision"),
                "entry_generation": approved.body.get("entry_generation"),
                "name": fixture.name,
                "source_ref": fixture.source_ref,
            }
        )
    return rows, refusals


def revoke_manifest(
    account: Account,
    rows: Sequence[Mapping[str, Any]],
    *,
    reason: str = REVOKE_REASON,
    opener: urllib.request.OpenerDirector | None = None,
) -> list[str]:
    refusals: list[str] = []
    for row in rows:
        revoked = post(
            account,
            f"/runbooks/{row['entry_id']}/revoke",
            {
                "idempotency_key": f"holmes-revoke-{uuid.uuid4().hex}",
                "revision": row["revision"],
                "expected_generation": row["entry_generation"],
                "reason": reason,
            },
            opener=opener,
        )
        if revoked.status != 200:
            refusals.append(f"revoke {row['name']}: {revoked.code or revoked.status}")
    return refusals


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="import_skills")
    commands = parser.add_subparsers(dest="command", required=True)
    importing = commands.add_parser("import")
    importing.add_argument("--question-dir", type=Path, required=True)
    importing.add_argument("--target", required=True)
    importing.add_argument("--manifest", type=Path, required=True)
    revoking = commands.add_parser("revoke")
    revoking.add_argument("--manifest", type=Path, required=True)
    revoking.add_argument("--reason", default=REVOKE_REASON)
    args = parser.parse_args(argv)
    account = Account.from_env(env)
    if args.command == "import":
        rows, refusals = import_question(account, args.question_dir, args.target)
        with args.manifest.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"imported {len(rows)} runbooks")
    else:
        rows = [
            json.loads(line)
            for line in args.manifest.read_text("utf-8").splitlines()
            if line.strip()
        ]
        refusals = revoke_manifest(account, rows, reason=args.reason)
        print(f"revoked {len(rows) - len(refusals)} runbooks")
    for refusal in refusals:
        print(refusal, file=sys.stderr)
    return 1 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
