"""Shared helpers for the #666 PR-D contract tests (C2's six runbook/skill
questions in the HolmesGPT eval driver and the Holmes comparison runner).

Written from docs/tasks/2026-11-18-holmes-evals-666.md (section "PR-D 接口")
and the C2 record (docs/tasks/2026-11-19-tool-expansion-c2.md, section 7)
without reading the PR-D implementation.

Question directories are synthetic (answer isolation, R8-6): a
``test_case.yaml`` with allowed keys plus a synthetic ``expected_output``
sentinel, and the knowledge fixtures (``*/SKILL.md`` or
``runbook_catalog.json`` + ``runbook_content_<id>.json``) whose bodies carry
a sentinel that must never reach evidence. The real pinned checkout is only
read for allowed keys (``memories_generated``, ``test_type``) and file names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.holmes_evals_support import write_question

Q89 = "89_skill_missing_cloudwatch"
Q90 = "90_skill_basic_selection"
Q94 = "94_skill_transparency"
Q95 = "95_skill_memory_leak_detection"
Q96 = "96_no_matching_skill"
Q162 = "162_get_skills"
C2_ALL = (Q89, Q90, Q94, Q95, Q96, Q162)
C2_K8S = (Q94, Q95, Q96, Q162)
C2_NO_K8S = (Q89, Q90)
NAMESPACE = {Q94: "app-94", Q95: "app-95", Q96: "app-96", Q162: "app-162"}
IMPORT_PREFIX = "holmes-eval-import-"

BODY_SENTINEL = "RUNBOOK-BODY-SENTINEL step three restart the pool"
GLOBAL_SENTINEL = "GLOBAL-INSTRUCTIONS-SENTINEL always page the CTO"
ISSUE_SENTINEL = "ISSUE-DATA-SENTINEL signup broken"
RESOURCE_SENTINEL = "RESOURCE-INSTRUCTIONS-SENTINEL check payments"

# Upstream-shaped prompts (``user_prompt`` is an allowed key).
PROMPTS = {
    Q89: "My AWS Lambda function 'payment-processor' in us-east-1 has high "
    "latency issues. Investigate why.",
    Q90: "Application-gateway server is returning 502 errors in the app-101 "
    "namespace. Please investigate.",
    Q94: "Database connection timeouts are occurring for blue-whale app in "
    "namespace app-94",
    Q95: "Debug memory issues for my Python application orca in namespace app-95",
    Q96: "Use the custom-crd runbook and check why custom CRD whale-watcher "
    "resources are not being reconciled by the operator in namespace app-96",
    Q162: "My signup service is failing and users can't register. What should "
    "I check to debug this issue?",
}
# Skill directories (SKILL.md questions) -> frontmatter name.
SKILLS = {
    Q89: [("lambda-performance-troubleshooting", "lambda-performance-troubleshooting")],
    Q90: [("application-gateway-troubleshooting", "application-gateway-troubleshooting")],
    Q94: [("database-troubleshooting", "database-troubleshooting")],
    Q95: [("python-memory-troubleshooting", "python-memory-troubleshooting")],
    Q96: [("service-troubleshooting", "service-troubleshooting")],
}  # fmt: skip
# 162: the DAL runbooks (title -> runbook name).
DAL = [
    ("8fe8e24d-6b53-47a6-92a5-b389938fa823", "Kafka Topic and Application Mapping",
     "Kafka lag issues, consumer falling behind"),
    ("40296b5c-2cb5-41df-b1f5-93441f894c44", "Pod Crashlooping Debugging Runbook",
     "Pod continuously crashing and restarting"),
    ("b2ffd311-f339-46a2-b379-1e53eb0bc1ed", "Signup Service Debugging Runbook",
     "Signup service failing, users can't register"),
]  # fmt: skip
DAL_NAMES = sorted(
    [
        "kafka-topic-and-application-mapping",
        "pod-crashlooping-debugging-runbook",
        "signup-service-debugging-runbook",
    ]
)


def fixture_names(qid: str) -> list[str]:
    if qid == Q162:
        return list(DAL_NAMES)
    return sorted(name for _, name in SKILLS[qid])


def write_c2_question(
    upstream: Path,
    qid: str,
    *,
    expected: str,
    fixtures: bool = True,
    extra: dict[str, Any] | None = None,
) -> Path:
    """A synthetic C2 question directory under ``upstream``."""
    doc: dict[str, Any] = {"user_prompt": PROMPTS[qid], "tags": ["skills"]}
    if qid in NAMESPACE:
        ns = NAMESPACE[qid]
        doc["before_test"] = f"kubectl create namespace {ns} || true\n"
        doc["after_test"] = f"kubectl delete namespace {ns} --force --grace-period=0\n"
    if qid == Q90:
        doc["include_tool_calls"] = True
    if qid == Q162:
        doc["test_type"] = "cluster"
    doc.update(extra or {})
    doc["expected_output"] = [expected]
    qdir = write_question(upstream, qid, doc)
    if not fixtures:
        return qdir
    if qid == Q162:
        (qdir / "runbook_catalog.json").write_text(
            json.dumps([{"id": i, "title": t, "symptom": s} for i, t, s in DAL])
        )
        for rid, title, symptom in DAL:
            (qdir / f"runbook_content_{rid}.json").write_text(
                json.dumps(
                    {
                        "title": title,
                        "symptom": symptom,
                        "instruction": f"{BODY_SENTINEL} ({title})",
                    }
                )
            )
        (qdir / "global_instructions.json").write_text(
            json.dumps({"instructions": [GLOBAL_SENTINEL]})
        )
        (qdir / "issue_data.json").write_text(json.dumps({"title": ISSUE_SENTINEL}))
        (qdir / "resource_instructions.json").write_text(
            json.dumps({"instructions": [RESOURCE_SENTINEL]})
        )
        return qdir
    for folder, name in SKILLS[qid]:
        path = qdir / folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: How to look at {name}\n---\n\n"
            f"# {name}\n\n{BODY_SENTINEL}\n"
        )
    return qdir
