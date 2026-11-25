"""Independent contract tests for the evaluation-fixture import script
(#566 C2b; C2 interface section 7 and 「C2b 签名级细节」, 导入脚本; pre-review
C2-12, C2-13).

``python -m scripts.holmes_evals.import_skills import --question-dir <dir>
--target <resource_uid> --manifest <path>`` and ``... revoke --manifest
<path> [--reason ...]`` talk only to the workbench's section 4 form API
(``Accept: application/json``) with credentials from the environment. Here
the workbench is a recording HTTP double that answers in the C2a JSON shape
(``RunbookResult.payload()`` plus ``replayed``); the real-app round trip is
``tests/acceptance/test_566_runbook_fetch_postgres.py``. Written without the
implementation; red until it lands.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.runbook_import_support import (
    ANSWER_MARKER,
    CONTENT_MARKER,
    DEFAULT_REASON,
    SKILLS,
    SOURCE_LABEL,
    SOURCE_REF_PREFIX,
    HttpFront,
    Recorded,
    allowed,
    file_sha256,
    manifest_lines,
    opened_inside,
    question_dir,
    run_script,
)

USER = "holmes-eval-import"
PASSWORD = "import-password-566-not-real"
ORIGIN = "https://ops.example.test"
TARGET = "m0-otel-20260909"


class FakeWorkbench:
    """Answers the three C2a write routes; refuses names in ``taken``."""

    def __init__(self) -> None:
        self.taken: set[str] = set()
        self.entries: dict[str, str] = {}
        self.submissions: list[str] = []

    def respond(self, recorded: Recorded, body: bytes):
        path = recorded.path.split("?", 1)[0]
        parts = path.strip("/").split("/")
        if recorded.method != "POST":
            return 405, {"content-type": "application/json"}, b'{"code":"NOT_FOUND"}'
        if parts == ["runbooks"]:
            if recorded.fields.get("name") in self.taken:
                return _json(409, {"code": "RUNBOOK_NAME_TAKEN"})
            sid = str(uuid4())
            self.submissions.append(sid)
            return _json(
                200,
                {
                    "action": "submit",
                    "object_kind": "runbook_submission",
                    "object_id": sid,
                    "generation": 1,
                    "submission_id": sid,
                    "entry_id": None,
                    "revision": None,
                    "entry_generation": None,
                    "state": "pending",
                    "replayed": False,
                },
            )
        if len(parts) == 4 and parts[:2] == ["runbooks", "submissions"]:
            sid, entry = parts[2], str(uuid4())
            self.entries[entry] = sid
            return _json(
                200,
                {
                    "action": "approve",
                    "object_kind": "runbook_submission",
                    "object_id": sid,
                    "generation": 2,
                    "submission_id": sid,
                    "entry_id": entry,
                    "revision": 1,
                    "entry_generation": 1,
                    "state": "approved",
                    "replayed": False,
                },
            )
        if len(parts) == 3 and parts[0] == "runbooks" and parts[2] == "revoke":
            return _json(
                200,
                {
                    "action": "revoke",
                    "object_kind": "runbook_entry",
                    "object_id": parts[1],
                    "generation": 2,
                    "submission_id": None,
                    "entry_id": parts[1],
                    "revision": int(recorded.fields.get("revision") or 0),
                    "entry_generation": None,
                    "state": "revoked",
                    "replayed": False,
                },
            )
        return _json(404, {"code": "NOT_FOUND"})


def _json(status: int, payload: dict) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "application/json"}, json.dumps(payload).encode()


@pytest.fixture
def bench():
    fake = FakeWorkbench()
    front = HttpFront(fake.respond)
    front.start()
    try:
        yield fake, front
    finally:
        front.stop()


def _env(front: HttpFront, **overrides: str | None) -> dict[str, str]:
    values: dict[str, str | None] = {
        "OPSPILOT_WEB_URL": front.url,
        "OPSPILOT_IMPORT_USER": USER,
        "OPSPILOT_IMPORT_PASSWORD": PASSWORD,
        "OPSPILOT_WEB_ORIGIN": ORIGIN,
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def _import(tmp_path: Path, front: HttpFront, qdir: Path, **env: str | None):
    manifest = tmp_path / "out" / "manifest.jsonl"
    manifest.parent.mkdir(exist_ok=True)
    args = [
        "import",
        "--question-dir",
        str(qdir),
        "--target",
        TARGET,
        "--manifest",
        str(manifest),
    ]
    run = run_script(args, _env(front, **env), cwd=tmp_path, log=tmp_path / "open.log")
    return run, manifest, args


def _assert_form_post(recorded: Recorded) -> None:
    assert recorded.method == "POST", recorded
    assert recorded.headers.get("origin") == ORIGIN, recorded.headers
    assert "application/json" in recorded.headers.get("accept", ""), recorded.headers
    assert "application/x-www-form-urlencoded" in recorded.headers.get(
        "content-type", ""
    )
    expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    assert recorded.headers.get("authorization") == expected
    assert recorded.fields.get("idempotency_key"), recorded.fields


def test_import_submits_and_approves_each_skill_through_the_form_api(tmp_path, bench):
    fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    run, manifest, args = _import(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    assert PASSWORD not in " ".join(args)

    for recorded in front.requests:
        _assert_form_post(recorded)
    submits = [r for r in front.requests if r.path == "/runbooks"]
    reviews = [r for r in front.requests if r.path.endswith("/review")]
    assert len(submits) == len(SKILLS) == len(reviews)
    assert len(front.requests) == 2 * len(SKILLS)
    by_name = {r.fields["name"]: r for r in submits}
    assert set(by_name) == set(SKILLS)
    for name, (description, body) in SKILLS.items():
        fields = by_name[name]
        assert fields.fields["resource_uid"] == TARGET
        assert fields.fields["description"] == description
        assert fields.fields["content"].strip() == body.strip()
        assert fields.fields["source_label"] == SOURCE_LABEL
        ref = fields.fields["source_ref"]
        skill = qdir / name / "SKILL.md"
        assert ref.startswith(SOURCE_REF_PREFIX), ref
        assert ref.endswith(f"#sha256={file_sha256(skill)}"), ref
        path_part = ref[len(SOURCE_REF_PREFIX) : ref.index("#sha256=")]
        assert path_part.endswith(f"{name}/SKILL.md"), ref
        assert not Path(path_part).is_absolute(), ref
        assert not fields.fields.get("supersedes_entry_id")
    # Each approval names a submission the script made, after making it.
    issued = 0
    for recorded in front.requests:
        if recorded.path == "/runbooks":
            issued += 1
            continue
        sid = recorded.path.split("/")[3]
        assert sid in fake.submissions[:issued], recorded.path
        assert recorded.fields["action"] == "approve"
        assert recorded.fields["expected_generation"].isdigit()
    assert sorted(r.path.split("/")[3] for r in reviews) == sorted(fake.submissions)


def test_the_manifest_lists_every_created_entry_outside_the_question_dir(
    tmp_path, bench
):
    fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    run, manifest, _args = _import(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    lines = manifest_lines(manifest)
    assert len(lines) == len(SKILLS)
    for line in lines:
        assert {"entry_id", "revision", "name", "source_ref"} <= set(line), line
        assert line["entry_id"] in fake.entries
        assert line["revision"] == 1
        assert line["source_ref"].startswith(SOURCE_REF_PREFIX)
    assert {line["name"] for line in lines} == set(SKILLS)
    assert not any(p.name == "manifest.jsonl" for p in qdir.rglob("*"))


def test_import_opens_only_skill_files_and_never_test_case_yaml(tmp_path, bench):
    _fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    run, _manifest, _args = _import(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    inside = opened_inside(run, qdir)
    assert inside, "the audit log saw no read of the question directory"
    for path in inside:
        assert allowed(path), path
    names = {p.name for p in inside}
    assert "test_case.yaml" not in names
    assert "global_instructions.json" not in names
    # Nothing planted in a forbidden file reached the workbench or the output.
    sent = json.dumps([r.fields for r in front.requests], ensure_ascii=False)
    assert ANSWER_MARKER not in sent
    assert ANSWER_MARKER not in run.output


def test_import_never_prints_runbook_bodies_or_the_password(tmp_path, bench):
    _fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    run, _manifest, _args = _import(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    assert CONTENT_MARKER not in run.output
    assert PASSWORD not in run.output


def test_a_refused_request_exits_1_and_prints_the_code_not_the_body(tmp_path, bench):
    fake, front = bench
    fake.taken.add("network-check")
    qdir = question_dir(tmp_path / "upstream")
    run, _manifest, _args = _import(tmp_path, front, qdir)
    assert run.returncode == 1, run.output
    assert "RUNBOOK_NAME_TAKEN" in run.output
    assert CONTENT_MARKER not in run.output


@pytest.mark.parametrize(
    "missing", ["OPSPILOT_IMPORT_PASSWORD", "OPSPILOT_IMPORT_USER", "OPSPILOT_WEB_URL"]
)
def test_without_its_environment_the_script_sends_nothing(tmp_path, bench, missing):
    _fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    run, _manifest, _args = _import(tmp_path, front, qdir, **{missing: None})
    assert run.returncode != 0, run.output
    assert front.requests == []
    assert PASSWORD not in run.output


def _revoke(tmp_path: Path, front: HttpFront, manifest: Path, *extra: str):
    args = ["revoke", "--manifest", str(manifest), *extra]
    return run_script(args, _env(front), cwd=tmp_path, log=tmp_path / "revoke.log")


def test_revoke_withdraws_every_manifest_entry_with_the_fixed_reason(tmp_path, bench):
    fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    run, manifest, _args = _import(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    entries = {line["entry_id"]: line for line in manifest_lines(manifest)}
    front.requests.clear()

    revoked = _revoke(tmp_path, front, manifest)
    assert revoked.returncode == 0, revoked.output
    assert {r.path for r in front.requests} == {
        f"/runbooks/{entry}/revoke" for entry in entries
    }
    for recorded in front.requests:
        _assert_form_post(recorded)
        entry = recorded.path.split("/")[2]
        assert recorded.fields["revision"] == str(entries[entry]["revision"])
        assert recorded.fields["reason"] == DEFAULT_REASON
        assert recorded.fields["expected_generation"].isdigit()
    assert CONTENT_MARKER not in revoked.output


def test_revoke_takes_another_reason(tmp_path, bench):
    _fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    run, manifest, _args = _import(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    front.requests.clear()
    revoked = _revoke(tmp_path, front, manifest, "--reason", "fixture withdrawn")
    assert revoked.returncode == 0, revoked.output
    assert front.requests
    assert {r.fields["reason"] for r in front.requests} == {"fixture withdrawn"}


# -- #780 (780-10): global instructions as standing instructions ---------------


def _import_instructions(tmp_path: Path, front: HttpFront, qdir: Path):
    manifest = tmp_path / "out" / "manifest.jsonl"
    manifest.parent.mkdir(exist_ok=True)
    args = ["import-instructions", "--question-dir", str(qdir), "--target", TARGET,
            "--manifest", str(manifest)]  # fmt: skip
    run = run_script(args, _env(front), cwd=tmp_path, log=tmp_path / "open.log")
    return run, manifest


def test_runbook_import_sends_no_kind(tmp_path, bench):
    _fake, front = bench
    run, _manifest, _args = _import(tmp_path, front, question_dir(tmp_path / "up"))
    assert run.returncode == 0, run.output
    assert all("kind" not in r.fields for r in front.requests)


def test_import_instructions_submits_one_standing_instructions_text(tmp_path, bench):
    fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    (qdir / "global_instructions.json").write_text(
        json.dumps({"instructions": ["first check", "  ", 7, "second check"]})
    )
    run, manifest = _import_instructions(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    inside = opened_inside(run, qdir)
    assert [p.name for p in inside] == ["global_instructions.json"]
    for recorded in front.requests:
        _assert_form_post(recorded)
    submit, review = front.requests
    assert submit.path == "/runbooks" and review.path.endswith("/review")
    fields = submit.fields
    assert fields["kind"] == "standing_instructions"
    assert fields["name"] == "standing-instructions"
    assert fields["resource_uid"] == TARGET
    assert fields["content"] == "* first check\n* second check\n"
    assert fields["source_ref"].endswith(
        "global_instructions.json#sha256="
        + file_sha256(qdir / "global_instructions.json")
    )
    (line,) = manifest_lines(manifest)
    assert line["entry_id"] in fake.entries and line["name"] == "standing-instructions"
    assert "first check" not in run.output


def test_import_instructions_over_the_limit_is_skipped_not_truncated(tmp_path, bench):
    _fake, front = bench
    qdir = question_dir(tmp_path / "upstream")
    (qdir / "global_instructions.json").write_text(
        json.dumps({"instructions": ["x" * 2100, "y" * 2100]})
    )
    run, manifest = _import_instructions(tmp_path, front, qdir)
    assert run.returncode == 0, run.output
    assert front.requests == []
    assert "GLOBAL_INSTRUCTIONS_TOO_LARGE" in run.output
    assert manifest_lines(manifest) == []
