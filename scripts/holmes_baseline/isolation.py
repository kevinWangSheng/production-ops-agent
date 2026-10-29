"""Isolation checklist for the Holmes baseline container (#399, contract U5).

``evaluate`` is pure: it turns one observation of the container into the
eight checks that decide whether #93's Holmes sub-item may be ticked.
``probe`` (engineer CLI, kind-lab window) observes the container: the same
``container.run_args`` shape as a real Holmes Run (image, user, read-only
root, no capabilities, no privilege gain, internal network, the two mounts,
the env names) with only the entrypoint replaced by ``PROBE_SCRIPT``.

  python -m scripts.holmes_baseline.isolation probe [--out <raw dir>]

The probe reports env var names only, never values; the two secret names are
given placeholder values, since nothing in the probe reads them. Raw output
stays under the git-ignored ``run.RAW_ROOT``; evidence goes to
``docs/evidence/f14-holmes-baseline/isolation-probe.json``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import kind_lab
from scripts.holmes_baseline import container

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/f14-holmes-baseline"
RECORDED_PROXY_LOG = EVIDENCE / "proxy-log-holmes.txt"
HOME = str(Path.home())

CHECK_IDS = (
    "uid_non_root",
    "forbidden_paths_absent",
    "env_names_allowlisted",
    "kube_api_unreachable",
    "prometheus_backend_unreachable",
    "internet_unreachable",
    "model_only_via_proxy",
    "proxy_log_allowed_paths",
)
# which ``observed`` key each check reads
CHECK_KEYS = (
    ("uid_non_root", "uid"),
    ("forbidden_paths_absent", "paths_present"),
    ("env_names_allowlisted", "env_names"),
    ("kube_api_unreachable", "reach"),
    ("prometheus_backend_unreachable", "reach"),
    ("internet_unreachable", "reach"),
    ("model_only_via_proxy", "reach"),
    ("proxy_log_allowed_paths", "proxy_log_lines"),
)
REACH_KEYS = (
    "kube_api",
    "prometheus_backend",
    "internet",
    "model_direct",
    "model_via_proxy",
)

_MAIN_CHECKOUT = str(Path.home() / "dev/AI/production-ops-agent")
# In-container paths that must not exist: the repo checkouts, env files, the
# lab auth file and kubeconfigs (incl. the in-cluster service-account mount),
# and the docker socket.
FORBIDDEN_PATHS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            str(ROOT),
            _MAIN_CHECKOUT,
            HOME,
            "/repo",
            "/workspace",
            f"{ROOT}/.env",
            f"{_MAIN_CHECKOUT}/.env",
            "/.env",
            "/app/.env",
            "/tmp/.env",
            "/out/.env",
            "/etc/holmes/.env",
            str(kind_lab.AUTH_FILE),
            f"{_MAIN_CHECKOUT}/tmp/m1-kind-lab/prometheus-auth.json",
            "/etc/opspilot-proxy",
            str(kind_lab.KUBECONFIG),
            f"{_MAIN_CHECKOUT}/tmp/m1-kind-lab/kubeconfig",
            f"{HOME}/.kube",
            "/root/.kube",
            "/tmp/.kube",  # HOME is /tmp in the container
            "/app/.kube",
            "/etc/kubernetes",
            "/var/run/secrets/kubernetes.io",
            "/var/run/docker.sock",
            "/run/docker.sock",
        ]
    )
)
# Names the pinned image (python:3.11-alpine base plus the Holmes Dockerfile;
# ``docker image inspect`` of sha256:3f2995d1a448...) and docker itself set.
IMAGE_ENV_NAMES: tuple[str, ...] = (
    "PATH",
    "LANG",
    "GPG_KEY",
    "PYTHON_VERSION",
    "PYTHON_SHA256",
    "PYTHONUNBUFFERED",
    "PYTHONPATH",
    "HOSTNAME",
)
# The readonly proxy's routes (kind_lab.proxy_nginx_config): GET/HEAD on the
# read paths and label values; POST forms only for holmes on query/range.
_GET_PATHS = frozenset(kind_lab.READ_PATHS)
_LABEL_VALUES = re.compile(r"^/api/v1/label/[A-Za-z_][A-Za-z0-9_]*/values$")
_POST_PATHS = frozenset({"/api/v1/query", "/api/v1/query_range"})


def _row(check_id: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"id": check_id, "passed": passed, "detail": detail}


def _uid(observed: Mapping[str, Any]) -> dict[str, Any]:
    uid = observed.get("uid")
    if type(uid) is not int:
        return _row("uid_non_root", False, f"uid missing or not an int: {uid!r}")
    return _row("uid_non_root", uid != 0, f"uid {uid}")


def _paths(observed: Mapping[str, Any]) -> dict[str, Any]:
    present = observed.get("paths_present")
    if not isinstance(present, list):
        return _row("forbidden_paths_absent", False, "paths_present missing")
    if present:
        return _row("forbidden_paths_absent", False, f"present: {sorted(present)}")
    return _row("forbidden_paths_absent", True, "none of the forbidden paths exist")


def _env(observed: Mapping[str, Any]) -> dict[str, Any]:
    names = observed.get("env_names")
    if not isinstance(names, list):
        return _row("env_names_allowlisted", False, "env_names missing")
    allowed = set(container.ENV_ALLOWLIST) | set(IMAGE_ENV_NAMES)
    # a "NAME=value" entry is a leak, not a name; only the name is reported
    bad = sorted(
        str(n).split("=", 1)[0] + ("=<value>" if "=" in str(n) else "")
        for n in names
        if not isinstance(n, str) or "=" in n or n not in allowed
    )
    if bad:
        return _row("env_names_allowlisted", False, f"not allowlisted: {bad}")
    return _row("env_names_allowlisted", True, f"{len(names)} names, all allowlisted")


def _reach(observed: Mapping[str, Any]) -> list[dict[str, Any]]:
    reach = observed.get("reach")
    reach = reach if isinstance(reach, Mapping) else {}

    def value(key: str) -> bool | None:
        v = reach.get(key)
        return v if isinstance(v, bool) else None

    rows = []
    for check_id, key in (
        ("kube_api_unreachable", "kube_api"),
        ("prometheus_backend_unreachable", "prometheus_backend"),
        ("internet_unreachable", "internet"),
    ):
        v = value(key)
        detail = f"{key} not observed" if v is None else f"{key} reachable: {v}"
        rows.append(_row(check_id, v is False, detail))
    direct, via = value("model_direct"), value("model_via_proxy")
    rows.append(
        _row(
            "model_only_via_proxy",
            direct is False and via is True,
            f"model direct reachable: {direct}; via egress proxy: {via}",
        )
    )
    return rows


def _proxy_line_ok(fields: list[str]) -> bool:
    if len(fields) < 5:
        return False
    _, _, method, path, status = fields[:5]
    if status == "403" or not status.isdigit():
        return False
    if method in ("GET", "HEAD"):
        return path in _GET_PATHS or bool(_LABEL_VALUES.match(path))
    return method == "POST" and path in _POST_PATHS


def _proxy_log(observed: Mapping[str, Any]) -> dict[str, Any]:
    lines = observed.get("proxy_log_lines")
    if not isinstance(lines, list):
        return _row("proxy_log_allowed_paths", False, "proxy_log_lines missing")
    holmes = [
        line.split()
        for line in lines
        if isinstance(line, str)
        and len(line.split()) > 1
        and line.split()[1] == "holmes"
    ]
    if not holmes:
        return _row("proxy_log_allowed_paths", False, "no holmes lines to judge")
    bad = [" ".join(f[2:5]) for f in holmes if not _proxy_line_ok(f)]
    if bad:
        return _row("proxy_log_allowed_paths", False, f"disallowed: {sorted(set(bad))}")
    return _row(
        "proxy_log_allowed_paths", True, f"{len(holmes)} holmes lines, all allowed"
    )


def evaluate(observed: dict[str, Any]) -> dict[str, Any]:
    """The checklist over one observation; a missing key fails its check."""
    checks = [
        _uid(observed),
        _paths(observed),
        _env(observed),
        *_reach(observed),
        _proxy_log(observed),
    ]
    assert [c["id"] for c in checks] == list(CHECK_IDS)
    return {"checks": checks, "passed": all(c["passed"] for c in checks)}


# --- the probe inside the container (stdlib only, prints one JSON line)

PROBE_SCRIPT = r"""
import errno, json, os, socket, subprocess, sys

t = json.loads(sys.argv[1])
timeout = t["timeout"]


def failure(exc):
    code = getattr(exc, "errno", None)
    if isinstance(exc, OSError) and not isinstance(exc, socket.gaierror):
        code = errno.errorcode.get(code, code)
    return {"error": type(exc).__name__, "errno": code}


def tcp(target):
    host, port = target.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"target": target, "ok": True}
    except Exception as exc:
        return {"target": target, "ok": False, **failure(exc)}


def connect(target):
    host, port = t["egress"].rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            line = s.recv(256).split(b"\r\n", 1)[0].decode("ascii", "replace")
            return {"target": target, "status": int(line.split()[1])}
    except Exception as exc:
        return {"target": target, "status": None, **failure(exc)}


def exists(path):
    try:
        os.lstat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None  # cannot tell (e.g. /root is 0700); the caller decides


try:
    uid = int(subprocess.run(["id", "-u"], capture_output=True, text=True,
                             timeout=5).stdout)
except Exception:
    uid = None
try:
    with open("/isolation-probe", "w"):
        pass
    root_write_denied = False
except OSError:
    root_write_denied = True
status = {}
with open("/proc/self/status") as f:
    for line in f:
        key, _, value = line.partition(":")
        if key in ("CapEff", "CapPrm", "CapBnd", "NoNewPrivs"):
            status[key] = value.strip()
with open("/proc/self/mounts") as f:
    mounts = sorted({line.split()[1] for line in f})
out = {
    "uid": uid,
    "env_names": sorted(os.environ),
    "paths": {p: exists(p) for p in t["paths"]},
    "tcp": {k: [tcp(x) for x in v] for k, v in t["tcp"].items()},
    "connect": {k: [connect(x) for x in v] for k, v in t["connect"].items()},
    "root_write_denied": root_write_denied,
    "proc_status": status,
    "mounts": mounts,
}
print("ISOLATION-PROBE " + json.dumps(out, sort_keys=True), flush=True)
"""
PROBE_MARK = "ISOLATION-PROBE "


def probe_args(image: str, *, targets: Mapping[str, Any], **kw: Any) -> list[str]:
    """The real Run's ``docker run`` with only the entrypoint and command
    replaced."""
    return container.run_args(
        image,
        entrypoint="python",
        command=["-c", PROBE_SCRIPT, json.dumps(targets, sort_keys=True)],
        **kw,
    )


# Paths the probe user could not stat, checked in the same image as root with
# no network, no mounts, no capabilities (root owns /root, so no DAC needed).
IMAGE_PATHS_SCRIPT = r"""
import json, os, sys


def exists(path):
    try:
        os.lstat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


print(json.dumps({p: exists(p) for p in json.loads(sys.argv[1])}))
"""


def image_check_args(image: str, paths: Sequence[str]) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "python",
        image,
        "-c",
        IMAGE_PATHS_SCRIPT,
        json.dumps(list(paths)),
    ]


def _under_mount(path: str, mounts: Sequence[str]) -> bool:
    return any(m != "/" and (path == m or path.startswith(m + "/")) for m in mounts)


def _present(
    path: str, seen: bool | None, mounts: Sequence[str], image: Mapping[str, Any]
) -> bool:
    """Fail closed: a path the probe could not stat is present unless the
    image shows it absent and no mount of the probe container covers it."""
    if seen is not None:
        return seen
    return not (image.get(path) is False and not _under_mount(path, mounts))


def observed_from_raw(
    raw: Mapping[str, Any],
    proxy_lines: Sequence[str],
    image_paths: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``evaluate``'s input from the probe's JSON line; a probe group that
    did not run leaves its reach key out (and its check fails)."""
    tcp = raw.get("tcp") if isinstance(raw.get("tcp"), Mapping) else {}
    conn = raw.get("connect") if isinstance(raw.get("connect"), Mapping) else {}
    via = conn.get("model_via_proxy") or []
    allowed = tcp.get("allowed") or []
    controls = bool(allowed) and all(r.get("ok") is True for r in allowed)
    controls = controls and any(r.get("status") == 200 for r in via)
    reach: dict[str, bool] = {}
    basis: dict[str, Any] = {}
    if any(r.get("status") == 200 for r in via):
        reach["model_via_proxy"] = True
    for key in ("kube_api", "prometheus_backend", "internet", "model_direct"):
        attempts = [*tcp.get(key, []), *conn.get(key, [])]
        reached = [r for r in attempts if r.get("ok") is True or r.get("status") == 200]
        unsure = [r for r in attempts if r not in reached and not _definitive(r)]
        if reached:
            reach[key] = True
            basis[key] = {"reachable": reached}
        elif not attempts:
            basis[key] = "not probed"
        elif unsure:
            basis[key] = {"indeterminate": unsure}
        elif not controls:
            basis[key] = "positive controls failed"
        else:
            reach[key] = False
            basis[key] = "every attempt failed definitively; controls passed"
    observed: dict[str, Any] = {
        "uid": raw.get("uid"),
        "paths_present": [
            p
            for p, v in (raw.get("paths") or {}).items()
            if _present(p, v, raw.get("mounts") or [], image_paths or {})
        ],
        "env_names": raw.get("env_names"),
        "reach": reach,
        "proxy_log_lines": list(proxy_lines),
        "probe": {"controls_passed": controls, "reach_basis": basis},
    }
    return observed


# Network-level outcomes that prove "no way there": no route (the --internal
# network has none), host refused, name not resolvable, or the egress proxy
# answered 403. A timeout is never definitive (a route may exist and only be
# slow), nor is any other exception or a missing result: those leave the
# reach key out, so its check fails.
DEFINITIVE_ERRNOS = frozenset({"ENETUNREACH", "EHOSTUNREACH", "ECONNREFUSED"})


def _definitive(attempt: Mapping[str, Any]) -> bool:
    if "status" in attempt:
        return attempt.get("status") == 403
    if attempt.get("ok") is not False:
        return False
    return (
        attempt.get("error") == "gaierror" or attempt.get("errno") in DEFINITIVE_ERRNOS
    )


def _home(value: Any) -> Any:
    """Host home directory as ``~`` in evidence."""
    return json.loads(json.dumps(value).replace(HOME, "~"))


# --- live probe (kind-lab window)


def _inspect(fmt: str, *target: str) -> str:
    from scripts.holmes_baseline import run

    return run.docker(*target[:-1], "inspect", "--format", fmt, target[-1]).strip()


def _kube_jsonpath(*args: str) -> str:
    proc = subprocess.run(
        kind_lab.kubectl(*args), capture_output=True, text=True, timeout=60
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def targets() -> dict[str, Any]:
    """What the probe tries, resolved on the host side (setup() is up)."""
    from scripts.holmes_baseline import config as hconfig
    from scripts.holmes_baseline import run

    node = run.KIND_NODE
    node_ip = _inspect('{{(index .NetworkSettings.Networks "kind").IPAddress}}', node)
    api_port = _inspect(
        '{{(index (index .NetworkSettings.Ports "6443/tcp") 0).HostPort}}', node
    )
    gateway = _inspect("{{(index .IPAM.Config 0).Gateway}}", "network", run.NETWORK)
    kube_svc = _kube_jsonpath(
        "-n", "default", "get", "svc", "kubernetes", "-o", "jsonpath={.spec.clusterIP}"
    )
    prom_svc = _kube_jsonpath(
        "-n", kind_lab.NAMESPACE, "get", "svc", "prometheus",
        "-o", "jsonpath={.spec.clusterIP}",
    )  # fmt: skip
    prom_pods = _kube_jsonpath(
        "-n", kind_lab.NAMESPACE, "get", "endpoints", "prometheus",
        "-o", "jsonpath={.subsets[*].addresses[*].ip}",
    ).split()  # fmt: skip
    # the model host's addresses as the egress proxy (outside) resolves them
    model_ips = json.loads(
        run.docker(
            "exec",
            run.EGRESS,
            "python",
            "-c",
            "import json,socket;print(json.dumps(sorted({a[4][0] for a in "
            f"socket.getaddrinfo({hconfig.MODEL_HOST!r},443,proto=6)}})))",
        )
    )
    model = f"{hconfig.MODEL_HOST}:443"
    return {
        "timeout": 3,
        "egress": f"{run.EGRESS}:3128",
        "paths": list(FORBIDDEN_PATHS),
        "tcp": {
            # positive controls: the two allowed ways out must answer
            "allowed": [
                f"{run.RELAY}:9090",
                f"{run.RELAY}:16686",
                f"{run.EGRESS}:3128",
            ],
            "kube_api": [
                f"{node}:6443",
                *([f"{node_ip}:6443"] if node_ip else []),
                *([f"{kube_svc}:443"] if kube_svc else []),
                *([f"{gateway}:{api_port}"] if gateway and api_port else []),
                f"{run.RELAY}:6443",
                "kubernetes.default.svc:443",
            ],
            "prometheus_backend": [
                "prometheus:9090",
                f"prometheus.{kind_lab.NAMESPACE}.svc.cluster.local:9090",
                *([f"{prom_svc}:9090"] if prom_svc else []),
                *[f"{ip}:9090" for ip in prom_pods],
            ],
            "internet": ["1.1.1.1:443", "8.8.8.8:53", "example.com:443"],
            "model_direct": [model, *[f"{ip}:443" for ip in model_ips]],
        },
        "connect": {
            "internet": ["example.com:443", "1.1.1.1:443", f"{hconfig.MODEL_HOST}:80"],
            "model_via_proxy": [model],
        },
    }


def cmd_probe(args: argparse.Namespace) -> int:
    from scripts.holmes_baseline import run

    pin = json.loads((EVIDENCE / "pin.json").read_text())
    image_id = json.loads(run.docker("image", "inspect", run.IMAGE))[0]["Id"]
    if image_id != pin["image_id"]:
        raise SystemExit(f"{run.IMAGE} is {image_id}, pin.json has {pin['image_id']}")
    out = Path(args.out) if args.out else run.RAW_ROOT / f"isolation-{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    config_path = run.write_config(out)
    env = {
        **run.docker_env(),
        # same names as a real Run; the probe never reads them
        "DEEPSEEK_API_KEY": "isolation-probe-placeholder",
        "HOLMES_PROMETHEUS_AUTHORIZATION": "isolation-probe-placeholder",
    }
    run.setup()
    try:
        probe_targets = targets()
        argv = probe_args(
            run.IMAGE,
            targets=probe_targets,
            network=run.NETWORK,
            egress_proxy=f"http://{run.EGRESS}:3128",
            config_path=str(config_path),
            output_dir=str(run.output_dir(out)),
            no_proxy=run.RELAY,
            name=run.HOLMES,
        )
        started = time.time()
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=300
        )
        egress = run.egress_log(run.EGRESS)
    finally:
        run.teardown()
    window_lines = run.proxy_log_lines(started)
    recorded = [
        line for line in RECORDED_PROXY_LOG.read_text().splitlines() if line.strip()
    ]
    shown = argv[: argv.index("-c") + 1] + ["<PROBE_SCRIPT>", "<targets>"]
    (out / "docker-run.argv.txt").write_text(" ".join(shown) + "\n")
    (out / "targets.json").write_text(json.dumps(probe_targets, indent=2) + "\n")
    (out / "probe.stdout.txt").write_text(proc.stdout)
    (out / "probe.stderr.txt").write_text(proc.stderr)
    (out / "egress.jsonl").write_text("".join(json.dumps(e) + "\n" for e in egress))
    (out / "proxy-window.txt").write_text("".join(f"{x}\n" for x in window_lines))
    marked = [x for x in proc.stdout.splitlines() if x.startswith(PROBE_MARK)]
    raw = json.loads(marked[-1][len(PROBE_MARK) :]) if marked else {}
    (out / "raw.json").write_text(json.dumps(raw, indent=2) + "\n")
    unreadable = [p for p, v in (raw.get("paths") or {}).items() if v is None]
    image_paths: dict[str, Any] = {}
    if unreadable:
        image_paths = json.loads(
            run.docker(*image_check_args(run.IMAGE, unreadable)[1:]).strip()
        )
    observed = observed_from_raw(raw, [*window_lines, *recorded], image_paths)
    observed["probe"].update(
        {
            "exit_code": proc.returncode,
            "valid": observed["probe"]["controls_passed"] and proc.returncode == 0,
            "positive_controls": {
                "tcp": (raw.get("tcp") or {}).get("allowed"),
                "model_via_proxy": (raw.get("connect") or {}).get("model_via_proxy"),
            },
            "targets": {"tcp": raw.get("tcp"), "connect": raw.get("connect")},
            "paths_checked": list(FORBIDDEN_PATHS),
            "paths_unreadable_by_probe_user": unreadable,
            "image_check_as_root_no_network": image_paths,
            "proxy_log_sources": {
                "probe_window": len(window_lines),
                "recorded_run": len(recorded),
                "recorded_run_file": RECORDED_PROXY_LOG.name,
            },
            "egress_proxy_log": [
                {k: e.get(k) for k in ("target", "allowed", "reason")}
                for e in egress
                if e.get("mode") == "connect" and "client" in e
            ],
            "container": {
                "root_write_denied": raw.get("root_write_denied"),
                "proc_status": raw.get("proc_status"),
                "mounts": raw.get("mounts"),
                "docker_run_argv": shown,
            },
        }
    )
    observed = _home(observed)
    evidence = {
        "image_id": image_id,
        "observed": observed,
        "result": evaluate(observed),
        "probed_at": dt.datetime.fromtimestamp(started, dt.timezone.utc).isoformat(),
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "isolation-probe.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "valid": observed["probe"]["valid"],
                "passed": evidence["result"]["passed"],
                "checks": {c["id"]: c["passed"] for c in evidence["result"]["checks"]},
            }
        )
    )
    return 0 if observed["probe"]["valid"] and evidence["result"]["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="observe the container in the kind lab")
    probe.add_argument("--out", help="raw output directory (git-ignored tmp)")
    probe.set_defaults(func=cmd_probe)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
