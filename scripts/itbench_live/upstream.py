"""The pinned ITBench checkout and its CLI (#564, PR 1).

Deployment and fault injection are the evaluation facility's actions
(PRODUCT-CONSTRAINTS: not an Agent permission) and go through ITBench's own
CLI only: ``uv run --no-dev itbench <command> ... --kubeconfig <admin>``.

Answer isolation (rental runbook, line 36): the scenario library holds the
answer next to each definition (``library/scenarios/<id>/ground_truth.yaml``,
written by upstream ``utils/ground_truth.py``), and the definition itself
(``scenario.yaml``, ``extra_variables.yaml``) names the injected fault. This
module only lists the directory names, checks that the expected files exist
by name, and hands out the ground-truth *root* path for the judging process.
It never opens a file under the library. The CLI's own output can carry the
same facts (Ansible task names, ``inject`` progress), so it goes to a log file
under the git-ignored round directory and is never printed; nothing in the
harness reads that log back. ``itbench explain`` and ``itbench list
scenarios`` print answers and are never called.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.itbench_lite.services import base_env

#: The upstream commit re-checked on 2026-11-24 (task record, 公开场景表).
ITBENCH_COMMIT = "5ed31cc4bef041af7f61120811ebff7f36612e4b"
#: The scenario table's applications all run here (upstream
#: ``project/roles/applications/defaults/main/opentelemetry_demo.yaml``).
APP_NAMESPACE = "otel-demo"
OBSERVABILITY_NAMESPACE = "itbench-observability"
#: ``kind create cluster --config clusters/kind/configs/environment.yaml``
#: names the cluster ``kind-environment``; kind prefixes the context.
EXPECTED_CONTEXT = "kind-kind-environment"
#: Two FinOps rows of the public table (no cost data in the product).
FINOPS_SCENARIOS = frozenset({"37", "38"})
GT_FILE = "ground_truth.yaml"
DEFINITION_FILE = "scenario.yaml"
DEPLOY_TIMEOUT_S = 3600.0
INJECT_TIMEOUT_S = 1800.0
REMOVE_TIMEOUT_S = 1800.0
UNDEPLOY_TIMEOUT_S = 3600.0
CONFIGURE_TIMEOUT_S = 600.0
#: ``itbench configure environment`` (upstream ``utils/cluster.py``) taints the
#: sandbox worker nodes with this key (``=true:NoSchedule``).
SANDBOX_TAINT_KEY = "itbench.io/sandbox"
_ID = re.compile(r"^[1-9][0-9]{0,3}$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class UpstreamError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def scenarios_root(checkout: Path) -> Path:
    return checkout / "library" / "scenarios"


def ground_truth_root(checkout: Path) -> Path:
    """The answer root, passed as a path to the judging process only."""
    return scenarios_root(checkout)


def scenario_ids(checkout: Path) -> list[str]:
    """SRE scenario ids that have a definition and an answer file, by name only.

    ``library/scenarios/<id>/`` directories whose name is a number and that
    contain both ``scenario.yaml`` and ``ground_truth.yaml`` (checked with
    ``is_file``, never opened); FinOps ids dropped; numeric order.
    """
    root = scenarios_root(checkout)
    if not root.is_dir():
        raise UpstreamError("SCENARIO_LIBRARY_MISSING", str(root))
    ids = [
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and _ID.match(entry.name)
        and (entry / DEFINITION_FILE).is_file()
        and (entry / GT_FILE).is_file()
        and entry.name not in FINOPS_SCENARIOS
    ]
    return sorted(ids, key=int)


def check_scenario(checkout: Path, scenario: str) -> None:
    if not _ID.match(scenario):
        raise UpstreamError("SCENARIO_ID_INVALID", scenario)
    if scenario in FINOPS_SCENARIOS:
        raise UpstreamError("SCENARIO_FINOPS", scenario)
    if scenario not in scenario_ids(checkout):
        raise UpstreamError("SCENARIO_UNKNOWN", scenario)


def check_pinned(checkout: Path, run: Runner = subprocess.run) -> str:
    done = run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    head = (done.stdout or "").strip()
    if done.returncode != 0 or head != ITBENCH_COMMIT:
        raise UpstreamError("ITBENCH_NOT_PINNED", head[:12] or "unknown")
    return head


def cli_argv(command: str, kubeconfig: Path, scenario: str | None = None) -> list[str]:
    """``uv run --no-dev itbench`` argv for the lifecycle commands."""
    base = ["uv", "run", "--no-dev", "itbench"]
    kube = ["--kubeconfig", str(kubeconfig)]
    if command == "configure":
        return [*base, "configure", "environment", *kube]
    if command == "deploy":
        if scenario is None:
            raise ValueError("deploy needs a scenario")
        return [*base, "deploy", "environment", "--scenario", scenario, *kube]
    if command == "inject":
        if scenario is None:
            raise ValueError("inject needs a scenario")
        return [*base, "inject", "faults", "--scenario", scenario, *kube]
    if command == "remove":
        return [*base, "remove", "faults", *kube]
    if command == "undeploy":
        return [*base, "undeploy", "environment", *kube, "--yes"]
    raise ValueError(f"unknown command {command!r}")


TIMEOUTS: Mapping[str, float] = {
    "configure": CONFIGURE_TIMEOUT_S,
    "deploy": DEPLOY_TIMEOUT_S,
    "inject": INJECT_TIMEOUT_S,
    "remove": REMOVE_TIMEOUT_S,
    "undeploy": UNDEPLOY_TIMEOUT_S,
}


def cli_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Base paths only: no model, LangSmith or judge credential reaches the
    upstream CLI. ``KUBECONFIG`` is not set; the path goes in ``--kubeconfig``."""
    env = base_env(os.environ if environ is None else environ)
    env.pop("KUBECONFIG", None)
    env["NO_COLOR"] = "1"
    return env


def kill_group(proc: subprocess.Popen[Any], grace: float = 10.0) -> None:
    """SIGTERM, then SIGKILL, to the child's whole process group (the child
    leads its own session), so ``uv``'s own children go too."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def run_group(
    argv: Sequence[str],
    *,
    capture_output: bool = False,
    check: bool = False,
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` with the child in a new session; on a timeout or any
    interruption (a signal raised in the driver) the whole group is killed,
    not only the direct child."""
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with subprocess.Popen(list(argv), start_new_session=True, **kwargs) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            kill_group(proc)
            raise
    done = subprocess.CompletedProcess(list(argv), proc.returncode, out, err)
    if check:
        done.check_returncode()
    return done


@dataclass(frozen=True)
class CliResult:
    command: str
    returncode: int
    seconds: float
    log: str


def run_cli(
    checkout: Path,
    command: str,
    kubeconfig: Path,
    log_path: Path,
    *,
    scenario: str | None = None,
    run: Runner = run_group,
    clock: Callable[[], float] | None = None,
) -> CliResult:
    """One CLI step; its output goes to ``log_path`` only (answer-bearing)."""
    import time

    clock = clock or time.monotonic
    argv = cli_argv(command, kubeconfig, scenario)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    began = clock()
    with log_path.open("ab") as log:
        try:
            done = run(
                argv,
                cwd=checkout,
                env=cli_env(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                check=False,
                timeout=TIMEOUTS[command],
            )
            code = int(done.returncode)
        except subprocess.TimeoutExpired:
            code = -1
    return CliResult(command, code, round(clock() - began, 3), str(log_path))


def require_ok(result: CliResult) -> CliResult:
    if result.returncode != 0:
        code = "TIMEOUT" if result.returncode == -1 else f"rc={result.returncode}"
        raise UpstreamError(f"ITBENCH_{result.command.upper()}_FAILED", code)
    return result


def leaks_upstream_paths(values: Sequence[str], checkout: Path) -> list[str]:
    """Values that point into the scenario library or name an answer file."""
    library = str(scenarios_root(checkout))
    return [v for v in values if library in v or GT_FILE in v or DEFINITION_FILE in v]


def sandbox_tainted_nodes(nodes: object) -> list[str]:
    """Worker nodes of a ``kubectl get nodes -o json`` body that carry the
    ITBench sandbox taint ``itbench.io/sandbox=true:NoSchedule``."""
    items = nodes.get("items") if isinstance(nodes, Mapping) else None
    found: list[str] = []
    for node in items if isinstance(items, list) else []:
        if not isinstance(node, Mapping):
            continue
        meta = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        spec = node.get("spec") if isinstance(node.get("spec"), Mapping) else {}
        taints = spec.get("taints") if isinstance(spec, Mapping) else None
        for taint in taints if isinstance(taints, list) else []:
            if (
                isinstance(taint, Mapping)
                and taint.get("key") == SANDBOX_TAINT_KEY
                and taint.get("value") == "true"
                and taint.get("effect") == "NoSchedule"
            ):
                name = meta.get("name") if isinstance(meta, Mapping) else None
                found.append(str(name))
                break
    return found
