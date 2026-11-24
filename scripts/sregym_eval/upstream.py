"""SREGym upstream pin and the Lite problem list (#667 S-1).

The checkout is never imported: HEAD is read from the git files, the pinned
files are hashed, and the Lite list is parsed out of ``problem_sets.py`` with
a regular expression. Problem ids name the fault mechanism (R8-6): they stay
on the evaluation side and never reach the product.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from scripts.holmes_evals.questions import UpstreamMismatch as _HolmesMismatch
from scripts.holmes_evals.questions import _head_commit

UPSTREAM_COMMIT = "ac60c642d94f8342e924014ddf36f5cdf15b3327"
PROBLEM_SETS_PATH = "sregym/conductor/problem_sets.py"
PINNED_FILES: Mapping[str, str] = {
    "sregym/conductor/oracles/llm_as_a_judge/judge.py": (
        "116e10ca9a156c1799ab51734b9cc2c7a4fae183fdf7fd12bb95f13064d9482f"
    ),
    "sregym/conductor/oracles/llm_as_a_judge/rca_checklists.yaml": (
        "4ca6522812892a3263abdc86d184a74c38769a4df0f56c29745fa2f787b10828"
    ),
    PROBLEM_SETS_PATH: (
        "032af668e3a1e582d29fcca2510e7eb9a2cb6d2fffe4e409d9e5a0315842c975"
    ),
    "sregym/conductor/conductor_api.py": (
        "20cdc78a92723038eaf3812079da8c2fbcd8160934a119f6d8e5833fb79addd7"
    ),
    "sregym/conductor/submission.py": (
        "36cd287d8da0af308f7d4354f6e743db6b2d4e13e5d85ec3ae87a18c0eae6cd4"
    ),
    "sregym/service/agent_visibility_policy.py": (
        "941c3501731a60a1368eab5f6947121ceb1ae284b708c24e234af239a2c491d2"
    ),
}
#: Upstream ``SREGYM_LITE_PROBLEMS`` at :data:`UPSTREAM_COMMIT`, upstream order.
LITE_PROBLEMS: tuple[str, ...] = (
    "cronjob_sidecar_blocks_completion_hotel_reservation",
    "edge_request_filter_cpu_saturation",
    "network_policy_block",
    "env_variable_shadowing_astronomy_shop",
    "mutating_webhook_resource_limits_social_network",
    "finalizer_deadlock_controller_hotel_reservation",
    "kafka_poison_pill_hol_block",
    "internal_traffic_policy_local_astronomy_shop",
    "service_dns_resolution_failure_social_network",
    "service_wrong_pod_selection_hotel_reservation",
    "namespace_memory_limit",
    "valkey_auth_disruption",
    "secret_rotation_stale_env_credentials_astronomy_shop",
    "unschedulable_incorrect_port_assignment",
    "duplicate_pvc_mounts_social_network",
    "rolling_update_misconfigured_social_network",
    "search_rate_retry_collapse_hotel_reservation",
)

_LITE_BLOCK = re.compile(r"^SREGYM_LITE_PROBLEMS\s*=\s*\((.*?)\)", re.S | re.M)
_STRING = re.compile(r"""("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')""")


class UpstreamMismatch(RuntimeError):
    """The checkout is not the pinned upstream; carries a fixed code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def problem_seq(problem_id: str) -> int:
    """1-based position in :data:`LITE_PROBLEMS`; ValueError when unknown."""
    try:
        return LITE_PROBLEMS.index(problem_id) + 1
    except ValueError:
        raise ValueError("PROBLEM_UNKNOWN") from None


def read_lite_problems(problem_sets_py: str) -> tuple[str, ...]:
    """The string literals of ``SREGYM_LITE_PROBLEMS = ( ... )``."""
    match = _LITE_BLOCK.search(problem_sets_py)
    if match is None:
        raise UpstreamMismatch("LITE_LIST_MISMATCH")
    body = "\n".join(line.split("#", 1)[0] for line in match.group(1).splitlines())
    items = tuple(str(ast.literal_eval(s)) for s in _STRING.findall(body))
    if not items:
        raise UpstreamMismatch("LITE_LIST_MISMATCH")
    return items


def verify_upstream(root: Path) -> None:
    """HEAD, then every pinned file, then the Lite list; first failure wins."""
    try:
        head = _head_commit(root)
    except _HolmesMismatch:
        raise UpstreamMismatch("UPSTREAM_HEAD_UNREADABLE") from None
    if head != UPSTREAM_COMMIT:
        raise UpstreamMismatch("UPSTREAM_HEAD_MISMATCH")
    for rel, digest in PINNED_FILES.items():
        try:
            data = (root / rel).read_bytes()
        except OSError:
            raise UpstreamMismatch(f"UPSTREAM_FILE_MISSING:{rel}") from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise UpstreamMismatch(f"UPSTREAM_FILE_MISMATCH:{rel}")
    text = (root / PROBLEM_SETS_PATH).read_text(encoding="utf-8")
    if read_lite_problems(text) != LITE_PROBLEMS:
        raise UpstreamMismatch("LITE_LIST_MISMATCH")
