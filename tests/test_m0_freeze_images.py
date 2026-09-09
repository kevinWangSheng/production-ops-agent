"""Offline CLI regressions: the archived experiment is never an output target."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
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
    # A tiny pinned source archive exercises the same mounted paths without downloads.
    source_name = "opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad"
    files = {
        "src/flagd/demo.flagd.json": b'{"flags": {}}\n',
        "src/grafana/grafana.ini": b"[server]\n",
        "src/grafana/provisioning/datasources/default.yaml": b"apiVersion: 1\n",
        "src/otel-collector/otelcol-config-extras.yml": b"# extras\n",
        "src/product-catalog/products/products.json": b"[]\n",
    }
    for name, content in files.items():
        path = runtime / source_name / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (runtime / source_name / "src/grafana/provisioning/empty").mkdir()
    with tarfile.open(runtime / "otel.tar.gz", "w:gz") as bundle:
        for path in sorted((runtime / source_name).rglob("*")):
            bundle.add(path, arcname=str(path.relative_to(runtime)), recursive=False)
    archive.joinpath("source-downloads.json").write_text(
        json.dumps(
            [
                {
                    "name": "otel",
                    "url": "https://codeload.github.com/open-telemetry/opentelemetry-demo/tar.gz/63649d6d6a59de88fb421b88c3c3a6185b6d21ad",
                    "sha256": hashlib.sha256(
                        (runtime / "otel.tar.gz").read_bytes()
                    ).hexdigest(),
                    "bytes": (runtime / "otel.tar.gz").stat().st_size,
                }
            ]
        )
    )
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


def run_freeze(root, *extra):
    return subprocess.run(
        [sys.executable, str(root / "scripts/m0_environment/freeze_images.py"), *extra],
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


SOURCE_NAME = "opentelemetry-demo-63649d6d6a59de88fb421b88c3c3a6185b6d21ad"


@pytest.mark.parametrize(
    "mutation",
    [
        "flagd",
        "added-file",
        "added-empty-dir",
        "removed-file",
        "removed-empty-directory",
        "ancestor-symlink",
        "nested-file-symlink",
        "grafana",
        "extras",
        "products",
        "file-symlink",
        "parent-symlink",
        "generated-symlink",
        "tar-drift",
    ],
)
def test_all_bind_inputs_are_checked_before_runtime_write(lab, mutation):
    root, _, _, _ = lab
    runtime = root / "tmp/m0-environment"
    source = runtime / SOURCE_NAME
    flag = source / "src/flagd/demo.flagd.json"
    if mutation == "flagd":
        flag.write_text('{"flags": {"changed": true}}')
    elif mutation == "added-file":
        (flag.parent / "extra.json").write_text("{}")
    elif mutation == "added-empty-dir":
        (flag.parent / "extra-directory").mkdir()
    elif mutation == "removed-file":
        flag.unlink()
    elif mutation == "removed-empty-directory":
        (source / "src/grafana/provisioning/empty").rmdir()
    elif mutation == "ancestor-symlink":
        target = root / "source-src"
        (source / "src").rename(target)
        (source / "src").symlink_to(target, target_is_directory=True)
    elif mutation == "nested-file-symlink":
        nested = source / "src/grafana/provisioning/datasources/default.yaml"
        target = root / "same-nested-content"
        target.write_bytes(nested.read_bytes())
        nested.unlink()
        nested.symlink_to(target)
    elif mutation in ("grafana", "extras", "products"):
        name = {
            "grafana": "src/grafana/grafana.ini",
            "extras": "src/otel-collector/otelcol-config-extras.yml",
            "products": "src/product-catalog/products/products.json",
        }[mutation]
        (source / name).write_text("changed")
    elif mutation == "file-symlink":
        target = root / "same-bytes"
        target.write_bytes(flag.read_bytes())
        flag.unlink()
        flag.symlink_to(target)
    elif mutation == "parent-symlink":
        target = root / "same-directory"
        flag.parent.rename(target)
        flag.parent.symlink_to(target, target_is_directory=True)
    elif mutation == "generated-symlink":
        original = runtime / "collector.yml"
        target = root / "same-generated-bytes"
        target.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(target)
    else:
        with (runtime / "otel.tar.gz").open("ab") as stream:
            stream.write(b"changed archive")
    before = snapshot(root)
    result = run_freeze(root)
    assert result.returncode != 0
    assert snapshot(root) == before


@pytest.mark.parametrize("location", ["outside", "inside-fixed-root"])
def test_unapproved_bind_is_rejected_even_if_compose_hash_matches(lab, location):
    root, _, expected_compose, _ = lab
    runtime = root / "tmp/m0-environment"
    # A FIFO outside the allowed source tree would block if opened as input.
    unknown = (
        root.parent if location == "outside" else runtime / SOURCE_NAME
    ) / "unapproved-input"
    os.mkfifo(unknown)
    candidate = json.loads((runtime / "compose.json").read_text())
    frozen = json.loads(expected_compose)
    for config in (candidate, frozen):
        config["services"]["grafana"]["volumes"][0]["source"] = str(unknown)
    (runtime / "compose.json").write_text(json.dumps(candidate))
    path = root / "docs/evidence/m0-real-environment/configuration-hashes.json"
    hashes = json.loads(path.read_text())
    hashes["tmp/m0-environment/compose-pinned.json"] = hashlib.sha256(
        (json.dumps(frozen, indent=2) + "\n").encode()
    ).hexdigest()
    path.write_text(json.dumps(hashes))
    before = snapshot(root)
    result = run_freeze(root)
    assert result.returncode != 0
    assert snapshot(root) == before
    assert (
        "Unapproved bind source" in result.stderr
        or "absent from fixed source archive" in result.stderr
    )


def test_check_only_validates_candidate_without_any_publication(lab):
    root, _, _, _ = lab
    before = snapshot(root)
    result = run_freeze(root, "--check-only")
    assert result.returncode == 0, result.stderr
    assert "9 bind inputs" in result.stdout
    assert snapshot(root) == before
