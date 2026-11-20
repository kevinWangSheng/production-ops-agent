"""A second SREGym filter proxy whose upstream is OpsPilot's read-only identity
(#667 S-U2 = c, decision table r11).

Run as a script inside SREGym's own environment::

    PYTHONPATH=<sregym checkout> KUBECONFIG=<read-only kubeconfig> \\
        uv run --directory <sregym checkout> python <this file> --port 26444 --out-dir <dir>

It builds upstream's ``KubernetesAPIProxy`` unchanged (same hidden
namespaces, labels and response filtering as every other SREGym agent sees),
except that ``KUBECONFIG`` -- the proxy's upstream credential -- is the
read-only ServiceAccount token, so the cluster itself refuses any write. The
first stdout line is ``{"ready": true, "kubeconfig": <path>}``; SIGTERM stops
it. The script part imports nothing from this repository.

The driver side (:func:`start_filter_proxy`, :func:`probe_product_path`)
imports the repository's helpers lazily, so the script stays standalone.
"""

from __future__ import annotations

import argparse
import base64
import json
import signal
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts.itbench_lite.services import Started
    from scripts.sregym_eval.identity import K8sTarget

KUBECONFIG_NAME = "opspilot-proxy-kubeconfig.yaml"
READY_TIMEOUT_S = 60.0


# -- script (runs under SREGym's interpreter) --------------------------------------------------


def _serve(port: int, out_dir: Path) -> int:
    from sregym.service.k8s_proxy import (  # type: ignore[import-not-found]
        KubernetesAPIProxy,  # upstream, unchanged
    )

    proxy = KubernetesAPIProxy(listen_port=port, listen_host="127.0.0.1")
    proxy.start()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = proxy.generate_agent_kubeconfig(str(out_dir / KUBECONFIG_NAME))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(json.dumps({"ready": True, "kubeconfig": str(path)}), flush=True)
    try:
        stop.wait()
    finally:
        proxy.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    return _serve(args.port, args.out_dir)


# -- driver side ----------------------------------------------------------------------------------


def proxy_argv(upstream: Path, port: int, out_dir: Path) -> list[str]:
    return [
        "uv", "run", "--directory", str(upstream), "python",
        str(Path(__file__).resolve()), "--port", str(port), "--out-dir", str(out_dir),
    ]  # fmt: skip


def proxy_env(upstream: Path, readonly_kubeconfig: Path) -> dict[str, str]:
    from scripts.itbench_lite.services import base_env

    return base_env() | {
        "PYTHONPATH": str(upstream),
        "KUBECONFIG": str(readonly_kubeconfig),
    }


def target_from_kubeconfig(path: Path, ca_out: Path) -> K8sTarget:
    """Server, CA and token of the proxy's generated kubeconfig."""
    import yaml  # type: ignore[import-untyped]

    from scripts.sregym_eval.identity import IdentityError, K8sTarget, private_write

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        (cluster,) = doc["clusters"]
        (user,) = doc["users"]
        server = str(cluster["cluster"]["server"])
        ca = base64.b64decode(cluster["cluster"]["certificate-authority-data"])
        token = str(user["user"]["token"])
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        raise IdentityError("PROXY_START_FAILED") from None
    if not token or not ca:
        raise IdentityError("PROXY_START_FAILED")
    private_write(ca_out, ca.decode("ascii", "replace"))
    return K8sTarget(api_url=server, ca_file=str(ca_out), token=token)


def start_filter_proxy(
    host: Any,
    upstream: Path,
    readonly_kubeconfig: Path,
    port: int,
    work_dir: Path,
) -> tuple[Started, K8sTarget]:
    """Start the proxy (``host.spawn``), wait for its ready line, return
    the product's target: the proxy's address, its self-signed CA, its token."""
    from scripts.itbench_lite import services
    from scripts.sregym_eval.identity import IdentityError

    out_dir = work_dir / "opspilot-proxy"
    spawn = getattr(host, "spawn", None) or services.spawn
    proc = spawn(
        proxy_argv(upstream, port, out_dir),
        proxy_env(upstream, readonly_kubeconfig),
        work_dir / "opspilot-proxy.log",
        stdout_pipe=True,
    )
    started = services.Started(
        "opspilot-proxy",
        lambda: services.stop_process(proc, 10),
        alive=lambda: proc.poll() is None,
    )
    try:
        line = json.loads(services._read_first_line(proc, READY_TIMEOUT_S))
        if not isinstance(line, dict) or line.get("ready") is not True:
            raise IdentityError("PROXY_START_FAILED")
        target = target_from_kubeconfig(
            Path(str(line["kubeconfig"])), out_dir / "proxy-ca.crt"
        )
    except IdentityError:
        started.stop()
        raise
    except BaseException:
        started.stop()
        raise IdentityError("PROXY_START_FAILED") from None
    started.info = {"port": port, "pid": proc.pid}
    return started, target


def http_probe(
    method: str, url: str, token: str, ca_file: str, body: bytes | None = None
) -> int:
    """Status of one request with the product's identity (no proxies, no
    redirects); a transport failure is 0."""
    context = ssl.create_default_context(cafile=ca_file)
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(request, timeout=15) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


_CONFIGMAP_BODY = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "opspilot-probe"},
}


def probe_product_path(host: Any, target: K8sTarget) -> dict[str, int]:
    """``GET /api/v1/pods?limit=1`` must be 200, a dry-run ConfigMap create
    403; otherwise ``PRODUCT_PATH_NOT_READONLY``."""
    from scripts.sregym_eval.identity import IdentityError

    base = target.api_url.rstrip("/")
    read = host.http_probe(
        "GET", f"{base}/api/v1/pods?limit=1", target.token, target.ca_file
    )
    write = host.http_probe(
        "POST",
        f"{base}/api/v1/namespaces/default/configmaps?dryRun=All",
        target.token,
        target.ca_file,
        json.dumps(_CONFIGMAP_BODY).encode("utf-8"),
    )
    result = {"list_pods": read, "dry_run_create_configmap": write}
    if read != 200 or write != 403:
        raise IdentityError("PRODUCT_PATH_NOT_READONLY")
    return result


if __name__ == "__main__":
    sys.exit(main())
