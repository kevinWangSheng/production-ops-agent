"""The isolated ``docker run`` for the pinned HolmesGPT image (U5-a, U8).

Non-root, read-only root filesystem, no capabilities, no privilege gain; an
internal Docker network on which only the lab relay (proxy + Jaeger) and the
allowlist egress proxy are reachable. Exactly two host mounts: the config
file read-only and the output directory. No docker.sock, kubeconfig, repo or
home mount. Environment names come from ``ENV_ALLOWLIST``; secret values are
passed by name only (``-e NAME``), so docker reads them from the caller's
environment and they never appear on argv.
"""

from __future__ import annotations

from collections.abc import Sequence

from scripts.holmes_baseline.config import MODEL_ENV

CONFIG_IN_CONTAINER = "/etc/holmes/config.yaml"
OUTPUT_IN_CONTAINER = "/out"
# nobody:nogroup in Alpine; the image has no other unprivileged user.
USER = "65534:65534"
SECRET_ENV = ("DEEPSEEK_API_KEY", "HOLMES_PROMETHEUS_AUTHORIZATION")
ENV_ALLOWLIST = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "HOME",
    *MODEL_ENV,
    *SECRET_ENV,
)


def run_args(
    image: str,
    *,
    network: str,
    egress_proxy: str,
    config_path: str,
    output_dir: str,
    no_proxy: str = "",
    name: str | None = None,
    command: Sequence[str] = (),
) -> list[str]:
    """``docker run`` argument list; ``command`` follows the image (the
    image's entrypoint is ``python holmes_cli.py``)."""
    env = {
        # every proxy-aware client (litellm/httpx, requests) leaves through the
        # egress proxy, except the lab relay named in NO_PROXY
        "HTTPS_PROXY": egress_proxy,
        "HTTP_PROXY": egress_proxy,
        "NO_PROXY": no_proxy,
        # read-only root: Holmes' caches and tool-result spill live in /tmp
        "HOME": "/tmp",
        **MODEL_ENV,
    }
    args = ["docker", "run", "--rm"]
    if name:
        args += ["--name", name]
    args += [
        "--network",
        network,
        "--user",
        USER,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "1g",
        "-v",
        f"{config_path}:{CONFIG_IN_CONTAINER}:ro",
        "-v",
        f"{output_dir}:{OUTPUT_IN_CONTAINER}:rw",
    ]
    for key, value in env.items():
        assert key in ENV_ALLOWLIST, key
        args += ["-e", f"{key}={value}"]
    for key in SECRET_ENV:
        args += ["-e", key]
    return [*args, image, *command]
