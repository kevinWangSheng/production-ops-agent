"""Fixed question template for the SREGym Lite track (#667 S-U1 = a, S-3).

SREGym has no incident text: ``GET /get_app`` gives an app name, its
namespace(s) and a static app description. The question is a fixed template
filled with exactly those fields; its sentences are upstream's
``clients/claudecode/driver.py`` ``build_instruction`` (ac60c642) with the
mitigation task, the submission mechanics and the kubectl/conductor lines
taken out. :data:`QUESTION_TEMPLATE_SHA256` pins the template; the result page
prints :data:`QUESTION_TEMPLATE` in full.

The problem id never enters the question or the idempotency key: it names
the fault mechanism, so for the product it is the answer (R8-6).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from scripts.sregym_eval.conductor import AppInfo

#: ``{namespace_block}`` is one of the two forms below.
QUESTION_TEMPLATE = """\
You are an SRE agent tasked with diagnosing issues in a Kubernetes application.

Application: {app_name}
{namespace_block}

{descriptions}

CRITICAL: You are running in an AUTOMATED environment. Work autonomously and \
make all decisions yourself. DO NOT ask for user confirmation or approval. \
Proceed with the best solution based on your analysis.

TASK: DIAGNOSIS
- Investigate the application to detect any anomalies or issues
- Analyze metrics, logs, and traces
- When ready, submit a natural language description of the issue you found
- Your diagnosis is evaluated on whether you correctly identify the faulty \
components and root cause

Important:
- You can query metrics and traces through the available observability tools
"""
NAMESPACE_SINGLE = "Namespace: {namespace}"
NAMESPACE_MULTI = (
    "Namespaces: {namespaces}\n"
    "(This scenario spans multiple namespaces; investigate all of them.)"
)
#: sha256 of ``json.dumps([QUESTION_TEMPLATE, NAMESPACE_SINGLE, NAMESPACE_MULTI])``.
QUESTION_TEMPLATE_SHA256 = (
    "b969b4e32ad6ea73dd628e88756bc75620cd23359ba0b60fc941d2bdc7ca3b8e"
)
#: The product's limit on a manual question (``INPUT_CONTENT_FIELD_MAX_CHARS``).
QUESTION_MAX_CHARS = 8192


class QuestionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AnswerLeak(RuntimeError):
    """A product-visible value carries the problem id (R8-6)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def template_sha256() -> str:
    """Recomputed digest of the three template parts."""
    blob = json.dumps([QUESTION_TEMPLATE, NAMESPACE_SINGLE, NAMESPACE_MULTI])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def render_question(app: AppInfo) -> str:
    """The question; reads only ``app_name``, ``namespaces``/``namespace``
    and ``descriptions``."""
    namespaces = tuple(n for n in app.namespaces if n) or (
        (app.namespace,) if app.namespace else ()
    )
    if not app.app_name or not namespaces:
        raise QuestionError("APP_INFO_INCOMPLETE")
    if len(namespaces) > 1:
        block = NAMESPACE_MULTI.format(namespaces=", ".join(namespaces))
    else:
        block = NAMESPACE_SINGLE.format(namespace=namespaces[0])
    text = QUESTION_TEMPLATE.format(
        app_name=app.app_name,
        namespace_block=block,
        descriptions=app.descriptions,
    )
    if len(text) > QUESTION_MAX_CHARS:
        raise QuestionError("QUESTION_TOO_LONG")
    return text


def idempotency_key(round: str, seq: int, trial: int) -> str:  # noqa: A002
    """``sregym-<round>-p<seq, two digits>-t<trial>``; no problem id (S-3)."""
    return f"sregym-{round}-p{seq:02d}-t{trial}"


def check_no_problem_id(problem_id: str, fields: Mapping[str, str]) -> None:
    """Refuse when any value contains the whole problem id (case-insensitive).

    Only the whole id is matched: its words (``social``, ``network``...) also
    appear in app names."""
    needle = problem_id.lower()
    if not needle:
        raise ValueError("PROBLEM_ID_EMPTY")
    for name, value in fields.items():
        if needle in str(value).lower():
            raise AnswerLeak(f"PROBLEM_ID_IN:{name}")
