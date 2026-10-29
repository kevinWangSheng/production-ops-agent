"""#394 publish workflow, static: only v* tags trigger it, it is the only
workflow holding ``packages: write``, actions are pinned by SHA, and it checks
tag/version and main ancestry before building amd64 + arm64 to GHCR."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"


def _load(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    assert isinstance(document, dict)
    return document


def _steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    (job,) = document["jobs"].values()
    return list(job["steps"])


def _step_index(steps: list[dict[str, Any]], needle: str) -> int:
    matches = [
        i
        for i, s in enumerate(steps)
        if needle in f"{s.get('uses', '')}\n{s.get('run', '')}"
    ]
    assert matches, needle
    return matches[0]


def test_triggers_only_on_v_tags() -> None:
    trigger = _load(RELEASE)[True]  # PyYAML reads ``on`` as True
    assert trigger == {"push": {"tags": ["v*"]}}


def test_permissions_read_at_top_and_package_write_only_on_the_job() -> None:
    document = _load(RELEASE)
    assert document["permissions"] == {"contents": "read"}
    (job,) = document["jobs"].values()
    assert job["permissions"] == {"contents": "read", "packages": "write"}


def test_no_other_workflow_mentions_package_write() -> None:
    others = [p for p in WORKFLOWS.glob("*.y*ml") if p != RELEASE]
    assert others
    for path in others:
        assert not re.search(r"packages\s*:\s*write", path.read_text(), re.I), path


def test_every_action_is_pinned_by_a_full_sha() -> None:
    for step in _steps(_load(RELEASE)):
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step[
                "uses"
            ]
    for line in RELEASE.read_text().splitlines():
        if re.search(r"\buses\s*:", line):
            assert re.search(r"@[0-9a-f]{40}\s*$", line), line


def test_checks_run_before_the_build_in_order() -> None:
    steps = _steps(_load(RELEASE))
    checkout = steps[_step_index(steps, "actions/checkout@")]
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False
    order = [
        _step_index(steps, "actions/checkout@"),
        _step_index(steps, 'check_version.py --tag "$GITHUB_REF_NAME"'),
        _step_index(steps, 'merge-base --is-ancestor "$GITHUB_SHA" origin/main'),
        _step_index(steps, "make check"),
        _step_index(steps, "docker/setup-qemu-action@"),
        _step_index(steps, "docker/setup-buildx-action@"),
        _step_index(steps, "docker/login-action@"),
        _step_index(steps, "docker/build-push-action@"),
        _step_index(steps, "GITHUB_STEP_SUMMARY"),
    ]
    assert order == sorted(order), order


def test_build_pushes_one_versioned_multi_arch_tag_with_min_provenance() -> None:
    steps = _steps(_load(RELEASE))
    build = steps[_step_index(steps, "docker/build-push-action@")]["with"]
    assert build["platforms"] == "linux/amd64,linux/arm64"
    assert build["push"] is True
    assert build["provenance"] == "mode=min"
    assert (
        build["tags"]
        == "ghcr.io/kevinwangsheng/opspilot:${{ steps.version.outputs.version }}"
    )
    assert (
        "OPSPILOT_VERSION=${{ steps.version.outputs.version }}" in build["build-args"]
    )
    assert "OPSPILOT_REVISION=${{ github.sha }}" in build["build-args"]
    login = steps[_step_index(steps, "docker/login-action@")]["with"]
    assert login["registry"] == "ghcr.io"
    assert login["password"] == "${{ secrets.GITHUB_TOKEN }}"


def test_no_latest_tag_no_attestation_and_no_other_secret() -> None:
    text = RELEASE.read_text()
    assert "latest" not in text.lower()
    assert "id-token" not in text and "attestations" not in text
    assert "attest-build-provenance" not in text
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GITHUB_TOKEN"}
