"""Optional Docker-gated runtime image checks for issue #394."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.environ.get("OPSPILOT_394_IMAGE_LIVE") != "1",
    reason="set OPSPILOT_394_IMAGE_LIVE=1 to run the Docker-gated image check",
)
def test_built_image_exposes_version_and_provenance_labels() -> None:
    if not shutil.which("docker"):
        pytest.skip("docker is unavailable")
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tag = f"opspilot:acceptance-394-{os.getpid()}"
    try:
        built = subprocess.run(
            [
                "docker",
                "build",
                "--build-arg",
                f"OPSPILOT_VERSION={version}",
                "--build-arg",
                f"OPSPILOT_REVISION={revision}",
                "--tag",
                tag,
                str(REPO),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        inspected = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", tag],
            capture_output=True,
            text=True,
            check=True,
        )
        labels = json.loads(inspected.stdout) or {}
        assert labels["org.opencontainers.image.version"] == version
        assert labels["org.opencontainers.image.revision"] == revision
        assert (
            labels["org.opencontainers.image.source"]
            == "https://github.com/kevinWangSheng/production-ops-agent"
        )

        runtime = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                tag,
                "python",
                "-c",
                "import opspilot; print(opspilot.__version__)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert runtime.returncode == 0, runtime.stdout + runtime.stderr
        assert runtime.stdout.strip() == version
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", tag], capture_output=True, check=False
        )
