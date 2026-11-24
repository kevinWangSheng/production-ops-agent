"""The bounded regex child of ``kubernetes_core.filter_lines`` (A3-7, A6).

Not imported by the product: ``filter_lines`` runs this file by its package
path as ``python3 -I -S -B <path>`` through ``run_isolated`` (no shell, an
empty environment, an empty working directory, a deadline, an output cap).
It imports only the standard library.

stdin: one JSON object ``{"ignore_case": bool, "include": str | null,
"exclude": str | null, "lines": [str, ...]}``. stdout: one JSON object with
``keep`` (one ``1``/``0`` per line), ``removed_include``, ``removed_exclude``
and, per pattern, ``include_mode``/``exclude_mode``: ``regex``, or
``substring`` when the pattern does not compile (matched as a substring with
the same case rule), or ``null`` when not given. A line is kept when it
matches ``include`` (if given) and does not match ``exclude`` (if given).
"""

from __future__ import annotations

import json
import re
import sys


def _compiled(
    pattern: str | None, flags: int
) -> tuple[re.Pattern[str] | None, str | None]:
    if pattern is None:
        return None, None
    try:
        return re.compile(pattern, flags), "regex"
    except (re.error, RecursionError, OverflowError, ValueError):
        return None, "substring"


def _hit(
    pattern: str, regex: re.Pattern[str] | None, text: str, ignore_case: bool
) -> bool:
    if regex is not None:
        return regex.search(text) is not None
    if ignore_case:
        return pattern.lower() in text.lower()
    return pattern in text


def main() -> None:
    spec = json.loads(sys.stdin.buffer.read())
    ignore_case = bool(spec["ignore_case"])
    flags = re.IGNORECASE if ignore_case else 0
    include = spec["include"]
    exclude = spec["exclude"]
    include_regex, include_mode = _compiled(include, flags)
    exclude_regex, exclude_mode = _compiled(exclude, flags)
    keep: list[str] = []
    removed_include = 0
    removed_exclude = 0
    for text in spec["lines"]:
        if include is not None and not _hit(include, include_regex, text, ignore_case):
            removed_include += 1
            keep.append("0")
        elif exclude is not None and _hit(exclude, exclude_regex, text, ignore_case):
            removed_exclude += 1
            keep.append("0")
        else:
            keep.append("1")
    result = {
        "keep": "".join(keep),
        "include_mode": include_mode,
        "exclude_mode": exclude_mode,
        "removed_include": removed_include,
        "removed_exclude": removed_exclude,
    }
    sys.stdout.buffer.write(json.dumps(result).encode("ascii"))


if __name__ == "__main__":
    main()
