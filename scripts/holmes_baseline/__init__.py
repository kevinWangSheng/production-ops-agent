"""F14 step 1 (#396): pinned HolmesGPT ``a0009195`` baseline harness.

Engineer-side only (PRODUCT-CONSTRAINTS allows an experiment harness); it is
not on any product path. ``config`` renders the Holmes config, ``container``
the isolated ``docker run`` arguments, ``project`` the IncidentOutcome-shaped
projection, ``relay`` the two helper processes (allowlist egress proxy and
lab TCP relay), ``run`` the engineer CLI. Contract:
docs/tasks/2026-10-27-m3-f14-upstream-contract.md (U1-U9, r1).
"""

from scripts.holmes_baseline.config import holmes_config
from scripts.holmes_baseline.container import run_args
from scripts.holmes_baseline.project import project_outcome

__all__ = ["holmes_config", "project_outcome", "run_args"]
