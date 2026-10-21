"""Independent black-box acceptance of the PR-D public interface r1.

No PR-D implementation imports. Mutations are confined to tmp_path copies.
Every script subprocess has sockets disabled, including the normal runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from opspilot.evaluation import (
    AttemptRecord,
    GateResult,
    Manifest,
    PlannedSlot,
    RunEvaluation,
    StorageError,
    aggregate,
    load_recording,
    replay,
)
from opspilot.evaluation.recording import blob, json_blob, unwire, wire

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/eval_structural"
NAME = "canary-fault-v1"
GENERATOR = ROOT / "scripts/eval_structural_fixture.py"
ENTRY = ROOT / "scripts/eval_structural_ci.py"
FIXTURE_SCHEMA = "opspilot-eval-structural-fixture-1"
CI_SCHEMA = "opspilot-eval-structural-ci-1"
KEYS = (
    "schema",
    "name",
    "recording_sha256",
    "attempt",
    "manifest",
    "expected_event_counts",
)
KINDS = ("model", "http", "tool", "delivery", "refusal")
CANARIES = {
    key: "-".join(("CANARY", name, "D", suffix))
    for key, name, suffix in (
        ("DEEPSEEK_API_KEY", "DEEPSEEK", "91b8"),
        ("OPENAI_API_KEY", "OPENAI", "76e1"),
        ("LANGSMITH_API_KEY", "LANGSMITH", "53f2"),
        ("AWS_SECRET_ACCESS_KEY", "AWS", "24c7"),
        ("OPSPILOT_DSN", "DSN", "82a9"),
    )
}
# A wrapper (rather than sitecustomize) also works with a cleared environment.
# Preserve socket's class shape so imports such as ssl can subclass it safely.
WRAPPER = r"""
import os, runpy, socket, sys

def forbidden(*args, **kwargs):
    sys.stderr.write("EVAL_D_FORBIDDEN_EXTERNAL_CALL\n")
    raise RuntimeError("EVAL_D_FORBIDDEN_EXTERNAL_CALL")

class ForbiddenSocket(socket.socket):
    def __new__(cls, *args, **kwargs):
        return forbidden(*args, **kwargs)

socket.socket = ForbiddenSocket
socket.create_connection = forbidden
script, root, readonly = sys.argv[1:4]
sys.path.insert(0, root)
sys.argv = [script, *sys.argv[4:]]

def audit(event, args):
    if event in ("socket.__new__", "socket.connect", "subprocess.Popen", "os.system"):
        forbidden()
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0]).replace("\\", "/")
        if os.path.basename(path).startswith(".env") or path.endswith((
            "/.aws/credentials", "/.aws/config", "/.netrc",
        )):
            sys.stderr.write("EVAL_D_FORBIDDEN_CREDENTIAL_READ\n")
            raise RuntimeError("EVAL_D_FORBIDDEN_CREDENTIAL_READ")
    if readonly == "1":
        writing = event in {
            "os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link",
            "os.symlink", "os.truncate", "os.chmod", "os.utime",
        }
        if event == "open":
            mode, flags = args[1:3]
            writing = (
                isinstance(mode, str) and any(c in mode for c in "wax+")
            ) or (
                isinstance(flags, int) and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                )
            )
        if writing:
            sys.stderr.write("EVAL_D_FORBIDDEN_FILE_WRITE\n")
            raise RuntimeError("EVAL_D_FORBIDDEN_FILE_WRITE")

sys.addaudithook(audit)
runpy.run_path(script, run_name="__main__")
"""


@pytest.fixture(autouse=True)
def require_public_scripts() -> None:
    missing = [
        str(path.relative_to(ROOT)) for path in (GENERATOR, ENTRY) if not path.is_file()
    ]
    assert not missing, "缺少脚本（预期 red）：" + ", ".join(missing)


@pytest.fixture
def frozen() -> Path:
    path = FIXTURES / NAME
    assert (path / "fixture.json").is_file(), "缺少入库 CANARY fixture"
    assert (path / "recording/recording.json").is_file()
    return path


def snapshot(path: Path) -> dict[str, bytes | None]:
    if not path.exists():
        return {}
    return {
        str(item.relative_to(path)): item.read_bytes() if item.is_file() else None
        for item in sorted(path.rglob("*"))
    }


def read_fixture(path: Path) -> dict[str, Any]:
    return json.loads((path / "fixture.json").read_bytes())


def write_fixture(path: Path, doc: dict[str, Any]) -> None:
    # Callers must always supply a tmp_path copy, never the frozen package.
    assert not path.resolve().is_relative_to(FIXTURES.resolve())
    (path / "fixture.json").write_text(json.dumps(doc), encoding="utf-8")


def copy_fixture(frozen: Path, destination: Path) -> Path:
    shutil.copytree(frozen, destination)
    return destination


def invoke(
    script: Path,
    args: list[str],
    tmp_path: Path,
    *,
    credentials: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    work = tmp_path / "process"
    home = work / "home"
    scratch = work / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(exist_ok=True)
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(scratch),
    }
    if credentials:
        environment.update(CANARIES)
    readonly = script == ENTRY
    before = (snapshot(FIXTURES), snapshot(tmp_path)) if readonly else None
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            WRAPPER,
            str(script),
            str(ROOT),
            str(int(readonly)),
            *args,
        ],
        cwd=cwd or tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert "EVAL_D_FORBIDDEN_" not in result.stderr, result.stderr
    if readonly:
        assert before == (snapshot(FIXTURES), snapshot(tmp_path)), "入口写入了文件"
    return result


def run_ci(
    directory: Path | None, tmp_path: Path, *, credentials: bool = False
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    result = invoke(
        ENTRY,
        [] if directory is None else ["--fixtures", str(directory)],
        tmp_path,
        credentials=credentials,
        cwd=ROOT if directory is None else None,
    )
    # json.loads rejects a second JSON value and any non-whitespace logging.
    report = json.loads(result.stdout)
    assert isinstance(report, dict)
    assert report["schema"] == CI_SCHEMA
    assert report["exit_code"] == result.returncode
    assert report["status"] in {"verified", "failed", "refused"}
    assert isinstance(report["results"], list)
    for item in report["results"]:
        assert {
            "name",
            "recording_sha256",
            "loaded",
            "structural_verified",
            "counts_ok",
            "error",
        } <= item.keys()
        for key in ("loaded", "structural_verified", "counts_ok"):
            assert type(item[key]) is bool
        assert item["error"] is None or isinstance(item["error"], str)
    return result, report


def assert_failed(
    result: subprocess.CompletedProcess[str],
    report: dict[str, Any],
    code: str | None = None,
) -> None:
    assert result.returncode == 1, result.stderr
    assert report["status"] == "failed"
    assert len(report["results"]) == 1
    error = report["results"][0]["error"]
    assert isinstance(error, str) and error
    if code:
        assert error == code


def generate(
    out: Path, tmp_path: Path, name: str = NAME
) -> subprocess.CompletedProcess[str]:
    return invoke(GENERATOR, ["generate", "--name", name, "--out", str(out)], tmp_path)


def test_generator_is_deterministic_and_matches_frozen(
    frozen: Path, tmp_path: Path
) -> None:
    trees = []
    for suffix in ("first", "second"):
        out = tmp_path / suffix
        result = generate(out, tmp_path)
        assert result.returncode == 0, result.stderr
        trees.append(snapshot(out / NAME))
    assert trees[0], "生成器没有产物"
    assert trees[0] == trees[1] == snapshot(frozen)


@pytest.mark.parametrize("existing", ["directory", "file"])
def test_generator_refuses_existing_target(tmp_path: Path, existing: str) -> None:
    target = tmp_path / "output" / NAME
    target.parent.mkdir()
    if existing == "directory":
        target.mkdir()
        (target / "CANARY-preserve").write_bytes(b"CANARY-existing")
    else:
        target.write_bytes(b"CANARY-existing")
    before = snapshot(target.parent)
    result = generate(target.parent, tmp_path)
    assert result.returncode == 2, result.stderr
    assert snapshot(target.parent) == before


@pytest.mark.parametrize("name", ["canary-unknown-d-91b8", "../CANARY-escape"])
def test_generator_refuses_unknown_name(tmp_path: Path, name: str) -> None:
    out = tmp_path / "output"
    out.mkdir()
    result = generate(out, tmp_path, name)
    assert result.returncode == 2, result.stderr
    assert snapshot(out) == {}
    assert not (tmp_path / "CANARY-escape").exists()


@pytest.mark.parametrize("default", [False, True])
def test_entry_verifies_frozen_and_is_readonly(
    frozen: Path, tmp_path: Path, default: bool
) -> None:
    result, report = run_ci(None if default else FIXTURES, tmp_path)
    assert result.returncode == 0, result.stderr
    assert report["status"] == "verified"
    names = [item["name"] for item in report["results"]]
    assert names == sorted(path.name for path in FIXTURES.iterdir() if path.is_dir())
    assert NAME in names
    for item in report["results"]:
        assert (
            item["loaded"] is item["structural_verified"] is item["counts_ok"] is True
        )
        assert item["error"] is None
        assert (
            item["recording_sha256"]
            == read_fixture(FIXTURES / item["name"])["recording_sha256"]
        )


def test_frozen_schema_and_recording_contents(frozen: Path) -> None:
    for directory in sorted(path for path in FIXTURES.iterdir() if path.is_dir()):
        doc = read_fixture(directory)
        assert set(doc) == set(KEYS)
        assert doc["schema"] == FIXTURE_SCHEMA
        assert doc["name"] == directory.name
        assert re.fullmatch(r"[a-z0-9-]{1,64}", doc["name"])
        assert re.fullmatch(r"[0-9a-f]{64}", doc["recording_sha256"])
        counts = doc["expected_event_counts"]
        assert set(counts) == set(KINDS)
        assert all(type(value) is int and value >= 0 for value in counts.values())
        attempt = unwire(AttemptRecord, doc["attempt"])
        manifest = unwire(Manifest, doc["manifest"])
        assert attempt.mode == "structural"
        assert attempt.product_run_id is None
        dimension = manifest.dimensions["replay_fixture"]
        assert (dimension.kind, dimension.value) == ("hash", doc["recording_sha256"])
        recording = load_recording(
            directory / "recording/recording.json",
            expected_hash=doc["recording_sha256"],
        )
        assert recording.context.synthetic is True
        assert counts == {
            kind: Counter(event.kind for event in recording.events)[kind]
            for kind in KINDS
        }
        capture = replay(
            recording,
            mode="structural",
            attempt=attempt,
            manifest=manifest,
            investigator=None,
        )
        assert capture.structural_verified is True
        assert capture.error is None
        if directory.name == NAME:
            events = list(recording.events)
            chain = iter(event.kind for event in events)
            assert all(
                any(actual == expected for actual in chain)
                for expected in ("model", "tool", "delivery", "model")
            )
            assert any(
                event.kind == "http" and event.response_ref is not None
                for event in events
            )


@pytest.mark.parametrize("kind", ["blob", "sha"])
def test_recording_tamper_is_rejected(frozen: Path, tmp_path: Path, kind: str) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    if kind == "blob":
        target = next(
            path
            for path in sorted((directory / "recording/blobs").rglob("*"))
            if path.is_file() and path.stat().st_size
        )
        data = target.read_bytes()
        target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    else:
        doc = read_fixture(directory)
        digest = doc["recording_sha256"]
        doc["recording_sha256"] = ("1" if digest[0] == "0" else "0") + digest[1:]
        write_fixture(directory, doc)
    expected_code = None
    if kind == "blob":
        doc = read_fixture(directory)
        with pytest.raises(StorageError) as error:
            load_recording(
                directory / "recording/recording.json",
                expected_hash=doc["recording_sha256"],
            )
        expected_code = error.value.code
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, expected_code)
    assert report["results"][0]["loaded"] is False


INVALID_FIELDS = [
    ("schema", "opspilot-eval-structural-fixture-999"),
    ("schema", None),
    ("name", ""),
    ("name", "CANARY-UPPER"),
    ("name", "canary_bad"),
    ("name", "x" * 65),
    ("name", "canary-different-directory"),
    ("name", "../canary"),
    ("name", 1),
    ("recording_sha256", "A" * 64),
    ("recording_sha256", "g" * 64),
    ("recording_sha256", "a" * 63),
    ("recording_sha256", None),
    ("attempt", {}),
    ("manifest", {}),
    ("expected_event_counts", {}),
]


@pytest.mark.parametrize(("key", "value"), INVALID_FIELDS)
def test_fixture_invalid_values(
    frozen: Path, tmp_path: Path, key: str, value: Any
) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    doc = read_fixture(directory)
    doc[key] = value
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "FIXTURE_INVALID")


@pytest.mark.parametrize("key", KEYS)
def test_fixture_missing_key(frozen: Path, tmp_path: Path, key: str) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    doc = read_fixture(directory)
    del doc[key]
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "FIXTURE_INVALID")


def test_fixture_extra_key(frozen: Path, tmp_path: Path) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    doc = read_fixture(directory)
    doc["CANARY-extra"] = None
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "FIXTURE_INVALID")


@pytest.mark.parametrize("payload", [b"{", b"[]", b"null", b"{} {}"])
def test_fixture_malformed_json(frozen: Path, tmp_path: Path, payload: bytes) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    (directory / "fixture.json").write_bytes(payload)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "FIXTURE_INVALID")


@pytest.mark.parametrize("name", ["a", "a" * 64])
def test_valid_fixture_name_boundaries(frozen: Path, tmp_path: Path, name: str) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / name)
    doc = read_fixture(directory)
    doc["name"] = name
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert result.returncode == 0, result.stderr
    assert report["status"] == "verified"
    assert [item["name"] for item in report["results"]] == [name]


@pytest.mark.parametrize("field", ["scenario_id", "scenario_version"])
def test_replay_checks_identity(frozen: Path, tmp_path: Path, field: str) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    doc = read_fixture(directory)
    attempt = unwire(AttemptRecord, doc["attempt"])
    doc["attempt"] = wire(replace(attempt, **{field: "CANARY-identity-mismatch"}))
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "IDENTITY_MISMATCH")
    assert report["results"][0]["loaded"] is True
    assert report["results"][0]["structural_verified"] is False


def test_saved_success_cannot_replace_current_verification(
    frozen: Path, tmp_path: Path
) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    # Deliberately plant a success-shaped cache, then remove source evidence.
    (directory / "structural-replay.json").write_text(
        json.dumps({"status": "verified", "structural_verified": True, "error": None})
    )
    (directory / "recording/recording.json").unlink()
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report)
    assert report["results"][0]["loaded"] is False


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "negative", "bool", "float", "string"]
)
def test_event_counts_strict_schema(
    frozen: Path, tmp_path: Path, mutation: str
) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    doc = read_fixture(directory)
    counts = doc["expected_event_counts"]
    if mutation == "missing":
        del counts["model"]
    elif mutation == "extra":
        counts["CANARY-extra"] = 0
    else:
        counts["model"] = {"negative": -1, "bool": True, "float": 1.0, "string": "1"}[
            mutation
        ]
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "FIXTURE_INVALID")


@pytest.mark.parametrize("kind", KINDS)
def test_counts_mismatch(frozen: Path, tmp_path: Path, kind: str) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    doc = read_fixture(directory)
    doc["expected_event_counts"][kind] += 1
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "EVENT_COUNTS_MISMATCH")
    assert report["results"][0]["counts_ok"] is False


@pytest.mark.parametrize("mutation", ["mode", "product_run_id", "fixture_dimension"])
def test_structural_metadata_is_required(
    frozen: Path, tmp_path: Path, mutation: str
) -> None:
    directory = copy_fixture(frozen, tmp_path / "fixtures" / NAME)
    doc = read_fixture(directory)
    if mutation == "fixture_dimension":
        doc["manifest"]["dimensions"]["replay_fixture"]["value"] = "f" * 64
    else:
        doc["attempt"][mutation] = (
            "live" if mutation == "mode" else "CANARY-run-forbidden"
        )
    write_fixture(directory, doc)
    result, report = run_ci(directory.parent, tmp_path)
    assert_failed(result, report, "FIXTURE_INVALID")


@pytest.mark.parametrize("state", ["absent", "empty", "files-only"])
def test_nothing_to_verify(tmp_path: Path, state: str) -> None:
    directory = tmp_path / "fixtures"
    if state != "absent":
        directory.mkdir()
    if state == "files-only":
        (directory / "CANARY-readme").write_text("CANARY")
    result, report = run_ci(directory, tmp_path)
    assert result.returncode == 3, result.stderr
    assert report["status"] == "refused"
    assert report["results"] == []


@pytest.mark.parametrize("bad_first", [False, True])
def test_mixed_results_fail_and_are_sorted(
    frozen: Path, tmp_path: Path, bad_first: bool
) -> None:
    parent = tmp_path / "fixtures"
    good_name, bad_name = (
        ("z-canary-good", "a-canary-bad")
        if bad_first
        else ("a-canary-good", "z-canary-bad")
    )
    for name, bad in ((good_name, False), (bad_name, True)):
        directory = copy_fixture(frozen, parent / name)
        doc = read_fixture(directory)
        doc["name"] = name
        if bad:
            doc["expected_event_counts"]["model"] += 1
        write_fixture(directory, doc)
    result, report = run_ci(parent, tmp_path)
    assert result.returncode == 1, result.stderr
    assert report["status"] == "failed"
    assert [item["name"] for item in report["results"]] == sorted((good_name, bad_name))
    by_name = {item["name"]: item for item in report["results"]}
    assert by_name[good_name]["error"] is None
    assert all(
        by_name[good_name][key] is True
        for key in ("loaded", "structural_verified", "counts_ok")
    )
    assert by_name[bad_name]["error"] == "EVENT_COUNTS_MISMATCH"


def test_offline_credentials_do_not_change_results(
    frozen: Path, tmp_path: Path
) -> None:
    plain, plain_report = run_ci(FIXTURES, tmp_path)
    # Poison both environment and conventional credential locations in the
    # subprocess's temporary HOME/cwd, never any developer credential file.
    home = tmp_path / "process/home"
    dotenv = "\n".join(f"{k}={v}" for k, v in CANARIES.items())
    for path in (tmp_path / ".env", tmp_path / ".env.judge", home / ".env"):
        path.write_text(dotenv)
    (home / ".aws").mkdir()
    (home / ".aws/credentials").write_text(
        "[default]\naws_secret_access_key=" + CANARIES["AWS_SECRET_ACCESS_KEY"]
    )
    poisoned, poisoned_report = run_ci(FIXTURES, tmp_path, credentials=True)
    assert plain.returncode == poisoned.returncode == 0
    assert plain_report == poisoned_report
    for data in snapshot(FIXTURES).values():
        if data is not None:
            assert all(value.encode() not in data for value in CANARIES.values()), (
                "fixture 包含假凭据"
            )


def test_generator_is_offline_and_credential_independent(
    frozen: Path, tmp_path: Path
) -> None:
    plain = generate(tmp_path / "plain", tmp_path)
    poisoned = invoke(
        GENERATOR,
        ["generate", "--name", NAME, "--out", str(tmp_path / "poisoned")],
        tmp_path,
        credentials=True,
    )
    assert plain.returncode == poisoned.returncode == 0
    assert (
        snapshot(tmp_path / "plain" / NAME)
        == snapshot(tmp_path / "poisoned" / NAME)
        == snapshot(frozen)
    )
    assert all(
        value.encode() not in data
        for data in snapshot(tmp_path / "poisoned" / NAME).values()
        if data is not None
        for value in CANARIES.values()
    )


def strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from strings(child)


def test_fixture_has_only_canary_hosts_and_identifiers(frozen: Path) -> None:
    doc = read_fixture(frozen)
    recording = load_recording(
        frozen / "recording/recording.json", expected_hash=doc["recording_sha256"]
    )
    values = [wire(recording.context)]
    for event in recording.events:
        for ref in (event.request_ref, event.response_ref):
            if ref is None:
                continue
            raw = blob(recording, ref)
            # JSON tool DTOs can contain hex-encoded raw evidence; inspect that
            # too, without assuming a private protocol field layout.
            try:
                values.append(json_blob(recording, ref))
            except (ValueError, UnicodeDecodeError):
                values.append(raw.decode("utf-8", errors="replace"))
    for value in values:
        for text in strings(value):
            candidates = [text]
            if re.fullmatch(r"(?:[0-9a-f]{2})+", text):
                candidates.append(bytes.fromhex(text).decode("utf-8", errors="replace"))
            for candidate in candidates:
                for url in re.findall(
                    r"https?://[^\s\"'<>\\]+", candidate, re.IGNORECASE
                ):
                    host = urlsplit(url).hostname
                    assert host and "canary" in host.lower(), (
                        "fixture 包含非 CANARY 主机"
                    )
                try:
                    nested = json.loads(candidate)
                except (ValueError, TypeError):
                    continue
                if isinstance(nested, dict):
                    assert_canary_identifiers(nested)
        if isinstance(value, dict):
            assert_canary_identifiers(value)


def assert_canary_identifiers(value: dict[str, Any]) -> None:
    fields = {
        "service",
        "service_name",
        "operation",
        "operation_name",
        "operation_id",
        "recording_id",
        "source_run_id",
        "scenario_id",
        "subject_id",
        "target_id",
        "evidence_id",
        "trace_id",
        "span_id",
        "tool_call_id",
    }
    for key, child in value.items():
        if key in fields and isinstance(child, str) and child:
            assert "canary" in child.lower(), f"{key} 必须使用 CANARY 合成标识"
        if isinstance(child, dict):
            assert_canary_identifiers(child)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    assert_canary_identifiers(item)


def test_structural_result_never_counts_as_quality(
    frozen: Path, tmp_path: Path
) -> None:
    result, report = run_ci(FIXTURES, tmp_path)
    assert result.returncode == 0
    item = next(item for item in report["results"] if item["name"] == NAME)
    doc = read_fixture(frozen)
    attempt = unwire(AttemptRecord, doc["attempt"])
    manifest = unwire(Manifest, doc["manifest"])
    evaluation = RunEvaluation(
        attempt=replace(attempt, started=True, status="ran"),
        gate=GateResult(item["structural_verified"], {}, ()),
        signature_matches={},
        judge_status="skipped",
        diagnosis_pass=None,
        evidence_status="complete",
        deterministic_pass=item["structural_verified"],
        judge_pass=False,
        overall=item["structural_verified"],
        failure_class=None,
        failure_reasons=(),
    )
    slot = PlannedSlot(
        attempt.scenario_id, attempt.scenario_version, attempt.mode, attempt.repetition
    )
    summary = aggregate(manifest, [slot], [evaluation], None)
    assert len(summary.rows) == 1
    assert summary.rows[0].mode == "structural"
    assert summary.rows[0].attempted == 1
    assert summary.rows[0].pass_rate is None
    assert summary.baseline_complete is False
    assert "NO_QUALITY_SLOTS" in summary.incomplete_reasons
