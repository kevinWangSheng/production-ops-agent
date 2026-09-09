"""Offline CLI regressions: the archived experiment is never an output target."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "docs/evidence/m0-real-environment"


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "isolated"
    scripts = root / "scripts/m0_environment"
    runtime = root / "tmp/m0-environment"
    archive = root / "docs/evidence/m0-real-environment"
    tools = root / "tools"
    for path in (scripts, runtime, archive, tools):
        path.mkdir(parents=True)
    shutil.copy(REPO / "scripts/m0_environment/freeze_images.py", scripts)
    proxy = b"print('test proxy source')\n"
    (scripts / "read_proxy.py").write_bytes(proxy)
    records = json.loads((EVIDENCE / "image-lock.json").read_text())
    frozen = json.loads(
        (EVIDENCE / "runtime-configuration/compose-pinned.json").read_text()
    )
    old_root = frozen["services"]["read-proxy"]["volumes"][0]["source"].removesuffix(
        "/tmp/m0-environment/read_proxy.py"
    )
    frozen = json.loads(json.dumps(frozen).replace(old_root, str(root)))
    expected_compose = (json.dumps(frozen, indent=2) + "\n").encode()
    candidate = copy.deepcopy(frozen)
    candidate["services"].pop("read-proxy")
    candidate["networks"].pop("investigation")
    for record in records:
        if record["service"] != "read-proxy":
            candidate["services"][record["service"]]["image"] = record["tag"]
    (runtime / "compose.json").write_text(json.dumps(candidate))
    for name in ("collector.yml", "prometheus.yaml"):
        shutil.copy(EVIDENCE / "runtime-configuration" / name, runtime / name)
    hashes = {
        "tmp/m0-environment/compose-pinned.json": hashlib.sha256(
            expected_compose
        ).hexdigest(),
        "tmp/m0-environment/read_proxy.py": hashlib.sha256(proxy).hexdigest(),
        **{
            "tmp/m0-environment/" + name: hashlib.sha256(
                (runtime / name).read_bytes()
            ).hexdigest()
            for name in ("collector.yml", "prometheus.yaml")
        },
    }
    # Deliberately use different whitespace: successful validation must not rewrite it.
    (archive / "image-lock.json").write_text(json.dumps(records, indent=1) + "\n")
    (archive / "configuration-hashes.json").write_text(
        json.dumps(hashes, indent=1) + "\n"
    )
    (runtime / "read_proxy.py").write_bytes(b"existing runtime proxy\n")
    (runtime / "compose-pinned.json").write_bytes(b"existing runtime compose\n")
    fixture = tools / "inspect.json"
    fixture.write_text(
        json.dumps(
            {
                record["tag"]: [
                    {
                        "RepoDigests": [record["repo_digest"]],
                        "Id": record["image_id"],
                        "Architecture": record["architecture"],
                        "Size": record["size"],
                    }
                ]
                for record in records
            }
        )
    )
    docker = tools / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        "assert sys.argv[1:5] == ['--context','colima-m0-otel','image','inspect']\n"
        "data=json.loads(Path(__file__).with_name('inspect.json').read_text())\n"
        "print(json.dumps(data[sys.argv[5]]))\n"
    )
    docker.chmod(0o755)
    return root, fixture, expected_compose, proxy


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for directory in (root / "docs", root / "tmp")
        for path in directory.rglob("*")
        if path.is_file()
    }


def run_freeze(root):
    return subprocess.run(
        [sys.executable, str(root / "scripts/m0_environment/freeze_images.py")],
        cwd=root,
        env={
            **os.environ,
            "PATH": str(root / "tools") + os.pathsep + os.environ["PATH"],
        },
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_same_images_materialize_runtime_without_rewriting_archive(lab):
    root, _, expected_compose, proxy = lab
    before = snapshot(root)
    result = run_freeze(root)
    assert result.returncode == 0, result.stderr
    after = snapshot(root)
    for name, content in before.items():
        if name.startswith("docs/"):
            assert after[name] == content
    assert (
        root / "tmp/m0-environment/compose-pinned.json"
    ).read_bytes() == expected_compose
    assert (root / "tmp/m0-environment/read_proxy.py").read_bytes() == proxy


@pytest.mark.parametrize("field", ["RepoDigests", "Architecture", "Id"])
def test_image_drift_is_rejected_before_any_runtime_or_archive_write(lab, field):
    root, fixture, _, _ = lab
    data = json.loads(fixture.read_text())
    # Change the last inspected image: earlier services must not cause partial writes.
    record = data["python:3.12.12-slim"][0]
    record[field] = {
        "RepoDigests": ["python@sha256:" + "f" * 64],
        "Architecture": "amd64",
        "Id": "sha256:" + "b" * 64,
    }[field]
    fixture.write_text(json.dumps(data))
    before = snapshot(root)
    result = run_freeze(root)
    assert result.returncode != 0
    assert snapshot(root) == before


@pytest.mark.parametrize("name", ["collector.yml", "read_proxy.py", "compose.json"])
def test_configuration_drift_is_rejected_before_publication(lab, name):
    root, _, _, _ = lab
    path = (
        root
        / (
            "scripts/m0_environment"
            if name == "read_proxy.py"
            else "tmp/m0-environment"
        )
        / name
    )
    if name == "compose.json":
        value = json.loads(path.read_text())
        value["services"]["prometheus"]["restart"] = "always"
        path.write_text(json.dumps(value))
    else:
        path.write_text(path.read_text() + "# changed\n")
    before = snapshot(root)
    result = run_freeze(root)
    assert result.returncode != 0
    assert snapshot(root) == before


@pytest.mark.parametrize("name", ["image-lock.json", "configuration-hashes.json"])
def test_missing_archive_is_not_silently_recreated(lab, name):
    root, _, _, _ = lab
    (root / "docs/evidence/m0-real-environment" / name).unlink()
    before = snapshot(root)
    result = run_freeze(root)
    assert result.returncode != 0
    assert snapshot(root) == before
