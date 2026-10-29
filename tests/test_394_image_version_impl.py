"""#394 version at runtime and image provenance: the Dockerfile writes the
OCI labels from build args, install.sh passes the pyproject version when it
builds opspilot:local and refuses any other product image not pinned by
digest (B9) before creating anything."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "compose" / "install.sh"
DOCKERFILE = ROOT / "Dockerfile"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
DIGEST = "sha256:" + "a" * 64

FAKE_DOCKER = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
"""


def test_dockerfile_labels_come_from_build_args() -> None:
    text = DOCKERFILE.read_text()
    runtime = text[text.index("AS runtime") :]
    assert re.search(r"^ARG OPSPILOT_VERSION$", runtime, re.M)
    assert re.search(r"^ARG OPSPILOT_REVISION$", runtime, re.M)
    labels = dict(re.findall(r'(org\.opencontainers\.image\.\w+)="([^"]*)"', runtime))
    assert labels == {
        "org.opencontainers.image.version": "${OPSPILOT_VERSION}",
        "org.opencontainers.image.revision": "${OPSPILOT_REVISION}",
        "org.opencontainers.image.source": "https://github.com/kevinWangSheng/production-ops-agent",
    }


@pytest.fixture
def run(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    home = tmp_path / "home"

    def go(
        image: str | None, *args: str
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "FAKE_DOCKER_LOG": str(log),
        }
        if image is not None:
            env["OPSPILOT_IMAGE"] = image
        result = subprocess.run(
            ["bash", str(INSTALL), *args],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        calls = (
            [json.loads(x) for x in log.read_text().splitlines()]
            if log.exists()
            else []
        )
        return result, calls

    go.home = home  # type: ignore[attr-defined]
    go.reset = lambda: log.unlink(missing_ok=True)  # type: ignore[attr-defined]
    return go


def test_local_build_passes_version_and_revision(run) -> None:
    result, calls = run(None)
    assert result.returncode == 0, result.stderr
    (build,) = [c for c in calls if c[:1] == ["build"]]
    assert f"OPSPILOT_VERSION={VERSION}" in build
    revision = next(a for a in build if a.startswith("OPSPILOT_REVISION="))
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert revision == f"OPSPILOT_REVISION={head}"
    assert build.count("--build-arg") == 2


@pytest.mark.parametrize(
    "image",
    [
        f"ghcr.io/kevinwangsheng/opspilot:{VERSION}",
        "ghcr.io/kevinwangsheng/opspilot:latest",
        "ghcr.io/kevinwangsheng/opspilot@sha256:abc",
        f"ghcr.io/kevinwangsheng/opspilot@{DIGEST}x",
        "opspilot:other",
    ],
)
def test_unpinned_product_image_is_refused_before_anything_is_created(
    run, image: str
) -> None:
    result, calls = run(image)
    assert result.returncode == 1
    assert "@sha256:" in result.stderr
    assert calls == []
    assert not run.home.exists()


@pytest.mark.parametrize(
    "image",
    [
        f"ghcr.io/kevinwangsheng/opspilot@{DIGEST}",
        f"ghcr.io/kevinwangsheng/opspilot:{VERSION}@{DIGEST}",
    ],
)
def test_digest_pinned_product_image_is_pulled_not_built(run, image: str) -> None:
    result, calls = run(image)
    assert result.returncode == 0, result.stderr
    assert ["pull", image] in calls
    assert not [c for c in calls if c[:1] == ["build"]]


UNPINNED = f"ghcr.io/kevinwangsheng/opspilot:{VERSION}"


@pytest.fixture
def recorded_unpinned(run) -> None:
    """An install whose install.env holds an image without a digest (as an
    install made before the refusal existed would)."""
    result, _ = run(None)
    assert result.returncode == 0, result.stderr
    settings = run.home / ".config" / "opspilot" / "install.env"
    text = settings.read_text()
    assert "OPSPILOT_IMAGE=opspilot:local\n" in text
    settings.write_text(
        text.replace("OPSPILOT_IMAGE=opspilot:local", f"OPSPILOT_IMAGE={UNPINNED}")
    )
    run.reset()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("uninstall",), ["down", "--remove-orphans"]),
        (("compose", "ps"), ["ps"]),
    ],
)
def test_uninstall_and_compose_work_with_a_recorded_unpinned_image(
    run, recorded_unpinned, args: tuple[str, ...], expected: list[str]
) -> None:
    result, calls = run(None, *args)
    assert result.returncode == 0, result.stderr
    (call,) = [c for c in calls if c[:1] == ["compose"]]
    assert call[-len(expected) :] == expected


def test_uninstall_works_with_an_unpinned_image_given_in_the_environment(
    run, recorded_unpinned
) -> None:
    result, calls = run(UNPINNED, "uninstall")
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c[:1] == ["compose"]]


@pytest.mark.parametrize("command", ["install", "upgrade"])
def test_install_and_upgrade_refuse_a_recorded_unpinned_image_before_any_docker_call(
    run, recorded_unpinned, command: str
) -> None:
    result, calls = run(None, command)
    assert result.returncode == 1
    assert "@sha256:" in result.stderr
    assert calls == []


def test_upgrade_refuses_an_unpinned_image_from_the_environment(
    run, recorded_unpinned
) -> None:
    result, calls = run(UNPINNED, "upgrade")
    assert result.returncode == 1
    assert "@sha256:" in result.stderr
    assert calls == []
