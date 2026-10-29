"""Dockerfile provenance-label contract checks for issue #394 (B1, B9)."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"
SOURCE = "https://github.com/kevinWangSheng/production-ops-agent"


def _runtime_stage() -> str:
    assert DOCKERFILE.is_file(), f"missing Dockerfile: {DOCKERFILE.relative_to(REPO)}"
    text = DOCKERFILE.read_text()
    starts = [match.start() for match in re.finditer(r"(?im)^\s*FROM\s", text)]
    assert starts, "Dockerfile has no FROM"
    # A build arg is visible to LABEL only when declared inside the final stage.
    return text[starts[-1] :]


def test_final_stage_declares_version_and_revision_build_args() -> None:
    stage = _runtime_stage()
    for name in ("OPSPILOT_VERSION", "OPSPILOT_REVISION"):
        assert re.search(rf"(?im)^\s*ARG\s+{name}(?:=\S*)?\s*$", stage), (
            f"final stage must declare ARG {name}"
        )


def test_final_stage_labels_version_revision_and_source() -> None:
    stage = _runtime_stage()
    assert re.search(r"(?im)^\s*LABEL\b", stage), "final stage must carry LABELs"
    version = r"org\.opencontainers\.image\.version\s*=\s*['\"]?\$\{?OPSPILOT_VERSION\}?['\"]?"
    revision = r"org\.opencontainers\.image\.revision\s*=\s*['\"]?\$\{?OPSPILOT_REVISION\}?['\"]?"
    source = (
        r"org\.opencontainers\.image\.source\s*=\s*['\"]?"
        + re.escape(SOURCE)
        + r"['\"]?"
    )
    assert re.search(version, stage), (
        "version OCI label must come from OPSPILOT_VERSION"
    )
    assert re.search(revision, stage), (
        "revision OCI label must come from OPSPILOT_REVISION"
    )
    assert re.search(source, stage), f"source OCI label must be {SOURCE}"
