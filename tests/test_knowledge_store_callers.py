"""Who may call which F13 store primitive (M1-03 D8 as revised, D6, D15).

Web and worker share one database role, so the database cannot tell them
apart; the split lives in code. The five review actions are called only by
the web layer (``opspilot/web/``); generation and staleness only outside it.
Static check over every product module but the store itself: any call of
these method names counts, whatever the receiver, so the rule cannot be
sidestepped by injecting the store under another name.
"""

import ast
import pathlib

from opspilot.knowledge import KnowledgeStore

ROOT = pathlib.Path(__file__).resolve().parents[1] / "opspilot"
REVIEW = {"approve", "reject", "return_for_revision", "revoke"}
WORKER = {"record_draft", "mark_stale"}


def _calls(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _modules() -> list[pathlib.Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if "migrations" not in path.parts and path.parent.name != "knowledge"
    ]


def test_the_primitives_exist_under_these_names() -> None:
    for name in REVIEW | WORKER:
        assert callable(getattr(KnowledgeStore, name)), name


def test_review_actions_are_called_only_from_the_web_layer() -> None:
    offenders = {
        str(path.relative_to(ROOT)): sorted(_calls(path) & REVIEW)
        for path in _modules()
        if "web" not in path.relative_to(ROOT).parts and _calls(path) & REVIEW
    }
    assert not offenders, offenders


def test_the_web_layer_never_generates_or_stales_a_draft() -> None:
    offenders = {
        str(path.relative_to(ROOT)): sorted(_calls(path) & WORKER)
        for path in _modules()
        if "web" in path.relative_to(ROOT).parts and _calls(path) & WORKER
    }
    assert not offenders, offenders
