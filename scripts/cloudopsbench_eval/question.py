"""Fixed question template for Cloud-OpsBench Runs (#871 U2 = a, U3).

The benchmark gives each case one symptom sentence (``query``) and a
namespace; the bundled agent's prompt adds the closed lists it must answer
from: List A, the 57 root-cause labels with their required object kind, and
List B, the valid node, application and namespace names
(``runtime/contracts.py`` ``build_expected_output``). The question carries
the same, verbatim (:mod:`scripts.cloudopsbench_eval.catalog`), plus the one
reporting rule the deterministic conversion needs (U3): the summary states
the chosen label and ``kind/name`` exactly as listed, and ``root_cause`` is
set to that object.

The first sentence is the bundled ``run.py`` task sentence. Only ``query``
and ``namespace`` vary per case; the case id never enters the question or
the idempotency key. :data:`QUESTION_TEMPLATE_SHA256` pins the template; the
longest question of both systems fits :data:`QUESTION_MAX_CHARS`
(``scripts.sregym_eval.question``, the product's manual-question limit).
"""

from __future__ import annotations

import hashlib
import json

from scripts.cloudopsbench_eval import catalog
from scripts.sregym_eval.question import QUESTION_MAX_CHARS

QUESTION_TEMPLATE = """\
The Kubernetes environment in namespace `{namespace}` is experiencing a fault. \
A high-level symptom has been reported: '{query}'. Diagnose the root cause of \
this incident.

The tools read a recorded snapshot of this incident. Identify the single most \
likely diagnosis: the root cause, chosen from List A, and the fault object, \
the object where that fault resides, chosen from List B. The fault object's \
kind must match the (Requires Target: ...) tag of the chosen root cause: \
APP is an application-level business service unit (its Kubernetes Service, \
Deployment and Pods are the same unit), NODE is a node, NAMESPACE is a \
namespace.

[List A: Valid Root Causes]
{root_causes}

[List B: Valid Resource Names]
- Nodes: {nodes}
- APP: {apps}
- Namespaces: {namespaces}

Reporting rule: when you conclude, write in the report summary the chosen \
root cause label exactly as it appears in List A and the fault object as \
kind/name, where kind is app, node or namespace and name is exactly as in \
List B, in this form: "Root cause: <label>; fault object: <kind>/<name>" \
(a node, for example, is written node/worker-01). \
Set root_cause to that object.
"""
#: sha256 over everything a question is made of except the case's ``query``
#: and ``namespace``: ``json.dumps([QUESTION_TEMPLATE, ROOT_CAUSE_LIST,
#: VALID_NODES, VALID_SERVICES, VALID_NAMESPACES])`` -- the template text and
#: both closed lists (List A labels, List B names), so a change to either
#: list moves the pin as an edit to the template does.
QUESTION_TEMPLATE_SHA256 = (
    "0d0f21085251a3c169983f18edb8c223a9e78f631eb580582c3f5fd0eefe919f"
)


class QuestionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def template_sha256() -> str:
    blob = json.dumps(
        [
            QUESTION_TEMPLATE,
            catalog.ROOT_CAUSE_LIST,
            catalog.VALID_NODES,
            catalog.VALID_SERVICES,
            catalog.VALID_NAMESPACES,
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _names(values: tuple[str, ...]) -> str:
    return ", ".join(values)


def render_question(system: str, *, query: str, namespace: str) -> str:
    """The question for one case; reads only ``query`` and ``namespace``."""
    if system not in catalog.VALID_SERVICES:
        raise QuestionError("SYSTEM_INVALID")
    if not query.strip() or not namespace.strip():
        raise QuestionError("CASE_FIELDS_MISSING")
    text = QUESTION_TEMPLATE.format(
        namespace=namespace.strip(),
        query=query.strip(),
        root_causes=catalog.ROOT_CAUSE_LIST.strip("\n"),
        nodes=_names(catalog.VALID_NODES),
        apps=_names(catalog.VALID_SERVICES[system]),
        namespaces=_names(catalog.VALID_NAMESPACES[system]),
    )
    if len(text) > QUESTION_MAX_CHARS:
        raise QuestionError("QUESTION_TOO_LONG")
    return text


def idempotency_key(round: str, seq: int, trial: int = 1) -> str:  # noqa: A002
    """``cob-<round>-c<seq, three digits>-t<trial>``: the position in the
    frozen list, never the case id."""
    return f"cob-{round}-c{seq:03d}-t{trial}"
