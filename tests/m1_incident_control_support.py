"""HTML readers for the incident-control contract tests.

They read only what the contract names: element ids, table header and cell
text, and the incident summary text ahead of the first ``<h2>``. They do not
depend on tag choice or CSS classes beyond ``badge``.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_VOID = {"br", "hr", "img", "input", "meta", "link"}


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.by_id: dict[str, list[str]] = {}
        self.badges: list[tuple[str | None, str]] = []
        self.rows: list[list[str]] = []
        self.headers: list[str] = []
        self.summary_nodes: list[tuple[str, tuple[str, ...]]] = []
        self._seen_h2 = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._cell_is_header = False

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        if tag == "h2":
            self._seen_h2 = True
        self.stack.append(
            {
                "tag": tag,
                "id": a.get("id"),
                "badge": "badge" in (a.get("class") or ""),
                "text": [],
            }
        )
        if tag == "tr":
            self._row = []
        if tag in {"td", "th"}:
            self._cell = []
            self._cell_is_header = tag == "th"

    def handle_data(self, data):
        for frame in self.stack:
            frame["text"].append(data)
        if self._cell is not None:
            self._cell.append(data)
        tags = {f["tag"] for f in self.stack}
        if (
            not self._seen_h2
            and "body" in tags
            and not tags & {"style", "script", "title"}
        ):
            ids = tuple(f["id"] for f in self.stack if f["id"])
            self.summary_nodes.append((data, ids))

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        while self.stack:
            frame = self.stack.pop()
            if frame["id"]:
                self.by_id.setdefault(frame["id"], []).append("".join(frame["text"]))
            if frame["badge"]:
                self.badges.append((frame["id"], "".join(frame["text"]).strip()))
            if frame["tag"] == tag:
                break
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = "".join(self._cell).strip()
            self._row.append(text)
            if self._cell_is_header:
                self.headers.append(text)
            self._cell = None
        if tag == "tr" and self._row is not None:
            if self._row and not self.headers_only(self._row):
                self.rows.append(self._row)
            self._row = None

    def headers_only(self, row):
        return row == self.headers[-len(row) :] and bool(self.headers)


def parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def element_text(html: str, element_id: str) -> str | None:
    """Stripped text of the element with this id; None when it is absent."""
    found = parse(html).by_id.get(element_id)
    if not found:
        return None
    assert len(found) == 1, f"id={element_id} appears {len(found)} times"
    return found[0].strip()


def summary_words(
    html: str, *, without_ids: tuple[str, ...] = ("run-state",)
) -> set[str]:
    """Lower-cased words of the incident summary (text before the first h2),
    ignoring text inside the named elements and the page title/heading."""
    page = parse(html)
    words: set[str] = set()
    for text, ids in page.summary_nodes:
        if any(i in ids for i in without_ids):
            continue
        words.update(re.findall(r"[a-z_]+", text.lower()))
    return words


def incident_badges(html: str) -> list[str]:
    """Text of every badge except the Run badge."""
    return [t for i, t in parse(html).badges if i != "run-state"]


def list_headers(html: str) -> list[str]:
    return parse(html).headers


def list_row(html: str, incident_id: str) -> dict[str, str]:
    page = parse(html)
    matches = [r for r in page.rows if incident_id in r[0]]
    assert len(matches) == 1, f"incident {incident_id} rows: {len(matches)}"
    assert len(matches[0]) == len(page.headers)
    return dict(zip(page.headers, matches[0]))
