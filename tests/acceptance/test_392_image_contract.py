"""Static contract checks for the #392 product image.

These tests deliberately inspect only the published build seams.  They do not
import product modules or depend on Docker, a registry, or credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _required(path: Path) -> str:
    assert path.is_file(), f"required #392 seam is missing: {path.relative_to(REPO)}"
    return path.read_text()


def _dockerfile_stages(text: str) -> tuple[list[str], set[str]]:
    images: list[str] = []
    aliases: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", line, re.I)
        if not match:
            continue
        images.append(match.group(1))
        if match.group(2):
            aliases.add(match.group(2).lower())
    return images, aliases


def test_dockerfile_is_multistage_digest_pinned_and_non_root() -> None:
    text = _required(REPO / "Dockerfile")
    images, aliases = _dockerfile_stages(text)

    assert len(images) >= 2, "the product image must use multiple build stages"
    external_images = [
        image
        for image in images
        if image.lower() not in aliases and image.lower() != "scratch"
    ]
    assert external_images, "Dockerfile has no external base image"
    assert all("@sha256:" in image for image in external_images), external_images
    users = [
        match.group(1)
        for line in text.splitlines()
        if (match := re.match(r"^\s*USER\s+(\S+)", line))
    ]
    assert users and users[-1] not in {"root", "0"}, (
        "the final image must run as a non-root user"
    )


def test_dockerfile_installs_only_locked_runtime_groups_and_exposes_all_roles() -> None:
    text = _required(REPO / "Dockerfile")
    lowered = text.lower()

    assert re.search(r"uv\s+sync\b", lowered), (
        "the image must install dependencies with uv sync"
    )
    assert "--locked" in lowered
    assert "--no-dev" in lowered
    assert "--all-groups" not in lowered
    assert "--group dev" not in lowered
    assert re.search(r"--group\s+m0\b", lowered)
    assert re.search(r"--group\s+web\b", lowered)

    for entrypoint in (
        "opspilot.web",
        "opspilot.worker_main",
        "opspilot.observer",
        "opspilot.schema migrate",
        "opspilot.retention",
    ):
        assert entrypoint in lowered, f"missing product role entrypoint: {entrypoint}"


@pytest.mark.parametrize(
    ("required_pattern", "accepted"),
    [
        (".env*", {".env*", "**/.env*", ".env", "**/.env"}),
        ("tmp/", {"tmp", "tmp/", "**/tmp", "**/tmp/"}),
        (
            "docs/evidence/",
            {
                "docs/evidence",
                "docs/evidence/",
                "**/docs/evidence",
                "**/docs/evidence/",
            },
        ),
        (".venv", {".venv", ".venv/", "**/.venv", "**/.venv/"}),
        (".git", {".git", ".git/", "**/.git", "**/.git/"}),
        (
            ".playwright-mcp",
            {
                ".playwright-mcp",
                ".playwright-mcp/",
                "**/.playwright-mcp",
                "**/.playwright-mcp/",
            },
        ),
    ],
)
def test_dockerignore_excludes_sensitive_or_local_material(
    required_pattern: str, accepted: set[str]
) -> None:
    text = _required(REPO / ".dockerignore")
    patterns = {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert patterns & accepted, f".dockerignore does not cover {required_pattern}"


def test_dockerfile_has_no_literal_credential_assignments() -> None:
    text = _required(REPO / "Dockerfile")
    secret_assignment = re.compile(
        r"^\s*(?:ENV|ARG)\s+"
        r"(?:[A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*)"
        r"\s*=\s*([^\s]+)",
        re.I,
    )
    for line in text.splitlines():
        match = secret_assignment.match(line)
        if match:
            value = match.group(1)
            assert value.startswith(("$", "${")), (
                "credential-like Dockerfile values must come from runtime secret plumbing"
            )
