"""SREGym agent registry for the OpsPilot entry (#667; upstream
``sregym/agent_registry.py``, read via ``SREGYM_AGENT_REGISTRY``).

The file replaces upstream's ``agents.yaml`` for the run, so it lists only
``opspilot``: a host process (``container_isolation: false``, like upstream's
tierzero), started with ``subprocess.Popen(shell=True)`` once the fault is in.

#830: the entry also names SREGym's ``hooks`` (``before_fault_injection``, run
in the conductor before the fault): ``opspilot_sregym_hooks``, which the driver
copies into the round directory and puts on ``main.py``'s ``PYTHONPATH``.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

AGENT_NAME = "opspilot"
ENTRY_MODULE = "scripts.sregym_eval.agent_entry"
HOOKS_SPEC = "opspilot_sregym_hooks:HOOKS"


def registry_document(python: str | Path, repo_root: str | Path) -> dict[str, Any]:
    return {
        "agents": [
            {
                "name": AGENT_NAME,
                "kickoff_command": f"{shlex.quote(str(python))} -m {ENTRY_MODULE}",
                "kickoff_workdir": str(repo_root),
                "kickoff_env": None,
                "install_script": None,
                "agent_version": None,
                "container_isolation": False,
                "hooks": HOOKS_SPEC,
            }
        ]
    }


def write_registry(path: Path, python: str | Path, repo_root: str | Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(registry_document(python, repo_root), sort_keys=False),
        encoding="utf-8",
    )
