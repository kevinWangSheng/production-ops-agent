"""Static publication-workflow contract checks for issue #394.

Contract: docs/tasks/2026-10-27-m3-f9-delivery-contract.md rows A2, B9, B10
and the #394 seams. The checks inspect workflow text and parsed YAML only;
they do not contact GitHub, Docker, a registry or any credential store.
Adopts the deferred #392 workflow assertions (single package publisher,
tag-only trigger, SHA-pinned actions, two platforms, lowercase GHCR name,
no ``latest``, repository token only).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
SHA_ACTION = re.compile(r"@[0-9a-f]{40}\s*(?:#.*)?$", re.I)
PACKAGES_WRITE = re.compile(r"(?im)^[^#\n]*\bpackages\s*:\s*['\"]?write\b")


def _load(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except Exception as exc:  # pragma: no cover - environment failure is a test failure
        pytest.fail(f"PyYAML is required to parse workflow contracts: {exc}")
    document = yaml.safe_load(path.read_text()) or {}
    assert isinstance(document, dict), path
    return document


def _workflow_documents() -> list[tuple[Path, dict[str, Any], str]]:
    assert WORKFLOWS.is_dir(), "the repository must have a workflow directory"
    paths = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))
    assert paths, "the repository must contain at least one workflow"
    return [(path, _load(path), path.read_text()) for path in paths]


def _trigger(document: dict[str, Any]) -> Any:
    # PyYAML 1.1 loads the YAML key ``on`` as True.
    return document.get("on", document.get(True))


def _grants_packages_write(permissions: Any) -> bool:
    if isinstance(permissions, str):
        return permissions.strip().lower() == "write-all"
    if isinstance(permissions, dict):
        return str(permissions.get("packages", "")).lower() == "write"
    return False


def _package_write_scopes(document: dict[str, Any]) -> list[str]:
    scopes = []
    if _grants_packages_write(document.get("permissions")):
        scopes.append("<workflow>")
    jobs = document.get("jobs")
    if isinstance(jobs, dict):
        for name, job in jobs.items():
            if isinstance(job, dict) and _grants_packages_write(job.get("permissions")):
                scopes.append(str(name))
    return scopes


def _release() -> tuple[dict[str, Any], str]:
    assert RELEASE.is_file(), "missing .github/workflows/release.yml"
    return _load(RELEASE), RELEASE.read_text()


def _publish_job() -> dict[str, Any]:
    document, _ = _release()
    jobs = document.get("jobs")
    assert isinstance(jobs, dict), jobs
    publishers = [
        job
        for job in jobs.values()
        if isinstance(job, dict) and _grants_packages_write(job.get("permissions"))
    ]
    assert len(publishers) == 1, (
        "release.yml must have exactly one job with packages: write"
    )
    return publishers[0]


def _step_texts(job: dict[str, Any]) -> list[str]:
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, "the publish job must have steps"
    return [json.dumps(step, sort_keys=True) for step in steps]


def _first_step(steps: list[str], pattern: str, what: str) -> int:
    for index, text in enumerate(steps):
        if re.search(pattern, text, re.I):
            return index
    pytest.fail(f"the publish job has no step that {what}")


def _without_comments(text: str) -> str:
    lines = (
        "" if line.lstrip().startswith("#") else re.sub(r"\s+#.*$", "", line)
        for line in text.splitlines()
    )
    return "\n".join(lines)


# -- permissions ---------------------------------------------------------------


def test_only_the_release_publish_job_holds_packages_write() -> None:
    documents = _workflow_documents()
    holders = {
        path.name: _package_write_scopes(document) for path, document, _ in documents
    }
    holders = {name: scopes for name, scopes in holders.items() if scopes}
    assert list(holders) == ["release.yml"], holders
    # Job-scoped, never workflow-wide, and only one job.
    assert len(holders["release.yml"]) == 1 and holders["release.yml"] != [
        "<workflow>"
    ], holders

    # Raw text as well, so an unexpected YAML shape cannot hide a grant.
    for path, _, text in documents:
        if path.name != "release.yml":
            assert not PACKAGES_WRITE.search(text), path


def test_release_permissions_are_contents_read_plus_job_packages_write() -> None:
    document, _ = _release()
    assert document.get("permissions") == {"contents": "read"}, document.get(
        "permissions"
    )
    # Job-level permissions replace the workflow-level set (B10: contents: read, packages: write).
    assert _publish_job().get("permissions") == {
        "contents": "read",
        "packages": "write",
    }


def test_release_requests_no_oidc_or_attestation_permissions() -> None:
    document, text = _release()
    for scope in [document, *document.get("jobs", {}).values()]:
        permissions = scope.get("permissions") if isinstance(scope, dict) else None
        if isinstance(permissions, dict):
            assert (
                "id-token" not in permissions and "attestations" not in permissions
            ), permissions
    assert "attest-build-provenance" not in _without_comments(text).lower()


# -- trigger and pinning -------------------------------------------------------


def test_release_triggers_only_on_version_tags() -> None:
    document, _ = _release()
    trigger = _trigger(document)
    assert isinstance(trigger, dict), trigger
    assert set(trigger) == {"push"}, trigger
    assert trigger["push"] == {"tags": ["v*"]}, trigger["push"]


def test_all_workflow_actions_are_pinned_to_full_commit_shas() -> None:
    for path, _, text in _workflow_documents():
        for line in text.splitlines():
            if re.search(r"^\s*-?\s*uses\s*:", line):
                assert SHA_ACTION.search(line), (
                    f"un-pinned action in {path.name}: {line.strip()}"
                )


# -- step order ----------------------------------------------------------------


def test_release_checks_version_main_ancestry_and_make_check_before_building() -> None:
    steps = _step_texts(_publish_job())
    checkout = _first_step(steps, r"actions/checkout@", "checks out the repository")
    assert re.search(r'"fetch-depth":\s*"?0\b', steps[checkout]), (
        "checkout must fetch full history"
    )

    version = _first_step(
        steps, r"check_version\.py", "runs scripts/release/check_version.py"
    )
    assert "--tag" in steps[version], steps[version]
    assert re.search(r"GITHUB_REF_NAME|github\.ref_name", steps[version]), steps[
        version
    ]

    fetch = _first_step(
        steps, r"git fetch\b[^\"]*\borigin\b[^\"]*\bmain\b", "fetches origin main"
    )
    ancestry = _first_step(
        steps, r"git merge-base --is-ancestor", "checks main ancestry"
    )
    assert re.search(r"GITHUB_SHA|github\.sha", steps[ancestry]), steps[ancestry]
    assert "origin/main" in steps[ancestry], steps[ancestry]

    setup = _first_step(steps, r"make setup", "runs make setup")
    check = _first_step(steps, r"make check", "runs make check")
    qemu = _first_step(steps, r"docker/setup-qemu-action@", "sets up QEMU")
    buildx = _first_step(steps, r"docker/setup-buildx-action@", "sets up Buildx")
    build = _first_step(
        steps, r"docker/build-push-action@", "builds and pushes the image"
    )

    assert checkout < version < build
    assert fetch <= ancestry < build
    assert setup < check < build
    assert qemu < build and buildx < build
    summary = _first_step(
        steps[build + 1 :], r"GITHUB_STEP_SUMMARY", "writes the job summary"
    )
    assert "digest" in steps[build + 1 + summary].lower(), (
        "the job summary must carry the pushed digest"
    )


# -- image, platforms and credentials ------------------------------------------


def test_release_pushes_lowercase_versioned_two_arch_image_never_latest() -> None:
    job = _publish_job()
    steps = _step_texts(job)
    build = steps[
        _first_step(steps, r"docker/build-push-action@", "builds and pushes the image")
    ]
    assert "linux/amd64" in build and "linux/arm64" in build, build
    assert re.search(r'"provenance":\s*"mode=min"', build), build
    assert re.search(r'"push":\s*(true|"true")', build), build

    job_text = json.dumps(job)
    assert "ghcr.io/kevinwangsheng/opspilot:" in job_text, (
        "image must be ghcr.io/kevinwangsheng/opspilot:<version>"
    )
    assert not re.search(r"ghcr\.io/[^\"\s:]*[A-Z]", job_text), (
        "GHCR names must be lowercase"
    )

    _, text = _release()
    meaningful = "\n".join(
        line
        for line in _without_comments(text).splitlines()
        if not re.match(r"\s*runs-on\s*:", line)
    )
    assert not re.search(r"\blatest\b", meaningful, re.I), (
        "release must never push or enable a latest tag"
    )


def test_release_uses_only_the_repository_token() -> None:
    _, text = _release()
    secret_names = set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", text))
    assert secret_names <= {"GITHUB_TOKEN"}, secret_names
    steps = _step_texts(_publish_job())
    login = steps[
        _first_step(steps, r"docker/login-action@|docker login", "logs in to GHCR")
    ]
    assert "ghcr.io" in login, login
    assert re.search(r"secrets\.GITHUB_TOKEN|github\.token", login), login
