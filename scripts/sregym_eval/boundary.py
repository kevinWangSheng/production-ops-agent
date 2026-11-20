"""Deterministic read-only boundary checks of the SREGym track (#667 S-5).

Layer 1: the RBAC manifest grants only get/list and names no secret, exec,
attach, port-forward, proxy, token or wildcard. Layer 2: ``kubectl auth
can-i`` answers for the read-only identity, judged by :func:`can_i_verdict`.
Layer 3: the product processes' environments hold no engineer, judge or
SREGym value (:func:`check_product_env`). Layer 4 is structural: the PR-A
conductor client can only submit the diagnosis stage.

Also here: the problem-id leak check on a digest (the agent entry never
receives the problem id itself).
"""

from __future__ import annotations

import hashlib
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

from scripts.sregym_eval.question import AnswerLeak, _squash

RBAC_ALLOWED_VERBS = frozenset({"get", "list"})
RBAC_FORBIDDEN_RESOURCES = frozenset(
    {
        "secrets",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "serviceaccounts/token",
        "*",
    }
)
_ROLE_KINDS = frozenset({"ClusterRole", "Role"})
READONLY_SUBJECT = "system:serviceaccount:opspilot-readonly:opspilot-investigator"


class BoundaryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# -- layer 1 -------------------------------------------------------------------------------------


def check_rbac_manifest(text: str) -> list[str]:
    """Violations of every ClusterRole/Role rule; empty means it passes."""
    try:
        documents = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError:
        return ["NO_ROLE"]
    roles = [
        d for d in documents if isinstance(d, dict) and d.get("kind") in _ROLE_KINDS
    ]
    if not roles:
        return ["NO_ROLE"]
    out: list[str] = []
    for role in roles:
        if role.get("aggregationRule") is not None:
            out.append("WILDCARD:aggregationRule")
        for rule in role.get("rules") or []:
            if not isinstance(rule, dict):
                out.append("NO_ROLE")
                continue
            for verb in rule.get("verbs") or []:
                if verb == "*":
                    out.append("WILDCARD:verbs")
                elif verb not in RBAC_ALLOWED_VERBS:
                    out.append(f"VERB:{verb}")
            for group in rule.get("apiGroups") or []:
                if group == "*":
                    out.append("WILDCARD:apiGroups")
            for resource in rule.get("resources") or []:
                if resource == "*" or str(resource).endswith("/*"):
                    out.append("WILDCARD:resources")
                elif resource in RBAC_FORBIDDEN_RESOURCES or str(resource).endswith(
                    "/proxy"
                ):
                    out.append(f"RESOURCE:{resource}")
            if rule.get("nonResourceURLs"):
                out.append("WILDCARD:nonResourceURLs")
            if rule.get("resourceNames") == ["*"]:
                out.append("WILDCARD:resourceNames")
    return out


# -- layer 2 -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CanICheck:
    verb: str
    resource: str
    expect: Literal["yes", "no"]

    @property
    def label(self) -> str:
        return f"{self.verb} {self.resource}"


def _no(verb: str, resource: str) -> CanICheck:
    return CanICheck(verb, resource, "no")


CAN_I_CHECKS: tuple[CanICheck, ...] = (
    *(
        _no(verb, resource)
        for resource in ("deployments.apps", "pods", "configmaps")
        for verb in ("create", "patch", "delete")
    ),
    _no("get", "secrets"),
    _no("list", "secrets"),
    _no("create", "pods/exec"),
    _no("create", "pods/portforward"),
    _no("create", "serviceaccounts/token"),
    CanICheck("list", "pods", "yes"),
    CanICheck("list", "deployments.apps", "yes"),
    CanICheck("list", "events", "yes"),
)


def can_i_argv(check: CanICheck, subject: str = READONLY_SUBJECT) -> list[str]:
    resource, _, sub = check.resource.partition("/")
    argv = ["kubectl", "auth", "can-i", check.verb, resource]
    if sub:
        argv.append(f"--subresource={sub}")
    return [*argv, "--all-namespaces", f"--as={subject}"]


def can_i_verdict(answers: Mapping[CanICheck, str]) -> list[str]:
    """Mismatches ``CAN_I:<verb>:<resource>:<answer>``; only ``yes``/``no``
    count as answers, anything else (an error, empty) is a mismatch, and so
    is a missing check."""
    out: list[str] = []
    for check in CAN_I_CHECKS:
        raw = answers.get(check)
        if raw is None:
            shown = "missing"
        else:
            answer = raw.strip() if isinstance(raw, str) else ""
            shown = answer if answer in ("yes", "no") else "invalid"
        if shown != check.expect:
            out.append(f"CAN_I:{check.verb}:{check.resource}:{shown}")
    return out


# -- layer 3 -------------------------------------------------------------------------------------

FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "KUBECONFIG",
    "JUDGE_",
    "SREGYM_",
    "MCP_SERVER_",
    "API_HOSTNAME",
    "API_PORT",
    "K8S_PROXY_",
    "AGENT_",
    "HARNESS_",
    "KIND_",
)
JUDGE_KEY_NAMES: tuple[str, ...] = ("ZAI_API_KEY", "GLM_API_KEY")
WEB_FORBIDDEN: tuple[str, ...] = (
    "OPSPILOT_K8S_TOKEN",
    "DEEPSEEK_API_KEY",
    "OPSPILOT_LOKI_BEARER_TOKEN",
    "OPSPILOT_LOKI_USERNAME",
    "OPSPILOT_LOKI_PASSWORD",
    "LANGSMITH_API_KEY",
)
_LOKI_URL = "OPSPILOT_LOKI_URL"


def check_product_env(
    env: Mapping[str, str],
    *,
    role: Literal["web", "worker"],
    forbidden_values: Sequence[str],
    backend_addresses: Sequence[str] = (),
) -> None:
    """Layer 3: raise ``ENV_ISOLATION:<role>:<key>`` on the first violation."""
    for key in env:
        if key.startswith(FORBIDDEN_ENV_PREFIXES) or key in JUDGE_KEY_NAMES:
            raise BoundaryError(f"ENV_ISOLATION:{role}:{key}")
    if role == "web":
        for key in WEB_FORBIDDEN:
            if key in env:
                raise BoundaryError(f"ENV_ISOLATION:{role}:{key}")
    needles = [v for v in forbidden_values if v]
    for key, value in env.items():
        if any(needle in str(value) for needle in needles):
            raise BoundaryError(f"ENV_ISOLATION:{role}:{key}")
    loki = env.get(_LOKI_URL, "")
    netloc = urllib.parse.urlsplit(loki).netloc.rpartition("@")[2] if loki else ""
    if netloc and netloc in set(backend_addresses):
        raise BoundaryError(f"ENV_ISOLATION:{role}:{_LOKI_URL}")


# -- problem-id digest ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProblemIdDigest:
    sha256: str
    length: int

    def as_json(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "length": self.length}

    @classmethod
    def from_json(cls, value: Any) -> ProblemIdDigest:
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("sha256"), str)
            or not isinstance(value.get("length"), int)
            or isinstance(value.get("length"), bool)
            or value["length"] <= 0
        ):
            raise ValueError("PROBLEM_DIGEST_INVALID")
        return cls(value["sha256"], value["length"])


def problem_id_digest(problem_id: str) -> ProblemIdDigest:
    squashed = _squash(problem_id)
    if not squashed:
        raise ValueError("PROBLEM_ID_EMPTY")
    return ProblemIdDigest(
        hashlib.sha256(squashed.encode("utf-8")).hexdigest(), len(squashed)
    )


def check_no_problem_id_digest(
    digest: ProblemIdDigest, fields: Mapping[str, str]
) -> None:
    """Sliding-window comparison on the same normalisation as PR-A's
    ``check_no_problem_id``; raises ``AnswerLeak("PROBLEM_ID_IN:<field>")``."""
    n = digest.length
    for name, value in fields.items():
        text = _squash(str(value))
        for start in range(0, len(text) - n + 1):
            window = text[start : start + n]
            if hashlib.sha256(window.encode("utf-8")).hexdigest() == digest.sha256:
                raise AnswerLeak(f"PROBLEM_ID_IN:{name}")
