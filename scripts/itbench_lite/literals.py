"""Safe parser for the list/dict cells of ITBench-Lite TSV exports (#624).

The OTel trace export writes array columns as Python reprs. Most are plain
literals (``['a', 'b']``, ``[{'k': 'v'}]``), but ``Events.Timestamp`` holds
numpy reprs such as ``[np.datetime64('2025-01-02T03:04:05.123456789')]``,
which ``ast.literal_eval`` rejects. ``parse_cell`` walks the syntax tree and
accepts plain literals plus exactly that one wrapper (returned as its string
argument); nothing is ever evaluated. Any other form raises ``ValueError``
naming the column.
"""

from __future__ import annotations

import ast
from typing import Any

#: ``np.datetime64('<text>')`` -> ``'<text>'``; the only call form in the data.
_WRAPPERS = frozenset({("np", "datetime64")})


def _wrapper_arg(node: ast.Call) -> str | None:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and (func.value.id, func.attr) in _WRAPPERS
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _convert(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_convert(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_convert(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise ValueError("dict unpacking")
        return {
            _convert(key): _convert(value)  # type: ignore[arg-type]
            for key, value in zip(node.keys, node.values, strict=True)
        }
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.USub, ast.UAdd))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ):
        value = node.operand.value
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Call):
        text = _wrapper_arg(node)
        if text is not None:
            return text
    raise ValueError(f"unsupported syntax {type(node).__name__}")


def parse_cell(text: str, column: str) -> Any:
    """The value of one list/dict cell; raises ``ValueError`` naming ``column``."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
        return _convert(tree.body)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError(f"column {column}: cannot parse cell: {exc}") from None
