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
        self.rows: list[dict[str, object]] = []
        self.headers: list[str] = []
        self.summary_nodes: list[tuple[str, tuple[str, ...]]] = []
        self._seen_h2 = False
        self._first_headers: list[str] | None = None
        self._thead_headers: list[str] | None = None
        self._in_thead = 0
        self._row: list[dict[str, object]] | None = None
        self._row_in_thead = False
        self._cell: list[str] | None = None
        self._cell_links: list[str] | None = None
        self._cell_colspan = 1
        self._cell_is_header = False

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        if tag == "thead":
            self._in_thead += 1
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
            self._row_in_thead = self._in_thead > 0
        if tag in {"td", "th"}:
            self._cell = []
            self._cell_links = []
            try:
                self._cell_colspan = max(1, int(a.get("colspan") or 1))
            except (TypeError, ValueError):
                self._cell_colspan = 1
            self._cell_is_header = tag == "th"
        if tag == "a" and self._cell_links is not None and a.get("href"):
            self._cell_links.append(a["href"])

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
            self._row.append(
                {
                    "text": "".join(self._cell).strip(),
                    "links": tuple(self._cell_links or ()),
                    "colspan": self._cell_colspan,
                    "is_header": self._cell_is_header,
                }
            )
            self._cell = None
            self._cell_links = None
            self._cell_colspan = 1
        if tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(
                    {"cells": self._row, "in_thead": self._row_in_thead}
                )
                row_headers = [
                    cell["text"] for cell in self._row if cell["is_header"]
                ]
                if row_headers:
                    if self._row_in_thead and self._thead_headers is None:
                        self._thead_headers = row_headers
                    elif not self._row_in_thead and self._first_headers is None:
                        self._first_headers = row_headers
                    self.headers = self._thead_headers or self._first_headers or []
            self._row = None
        if tag == "thead":
            self._in_thead = max(0, self._in_thead - 1)


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
    href = f"/incidents/{incident_id}"
    matches = [
        row
        for row in page.rows
        if any(
            href == link
            for cell in row["cells"]
            for link in cell["links"]
        )
    ]
    assert len(matches) == 1, f"incident {incident_id} rows: {len(matches)}"
    assert page.headers, "incident list has no table headers"
    values: list[str] = []
    for cell in matches[0]["cells"]:
        values.append(cell["text"])
        values.extend([""] * (cell["colspan"] - 1))
    assert len(values) == len(page.headers), (
        f"incident {incident_id} cells: {len(values)}, "
        f"headers: {len(page.headers)}"
    )
    return dict(zip(page.headers, values))
