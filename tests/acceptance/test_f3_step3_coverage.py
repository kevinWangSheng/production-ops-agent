"""Independent acceptance tests for F3 step 3 (PR-2): the coverage-gap section
and the truncated-at-limit annotation.

Written by a fresh-context author who did not implement PR-2 and did not read
the implementation branch, from the contract pre-review
``~/.agent/handoffs/opspilot-prereview-f3-pr2-2026-10-24.md`` (D1-D9 as
recommended, R-1..R-16 as written), section 7 items 1-17.

This module holds what needs no database: the public projection types (R-9)
and the model-visible surface guard (item 17: no revision moves). It also
holds the page-reading helpers the PostgreSQL module
``tests/integration/test_f3_step3_coverage_postgres.py`` uses for items 1-16
on the product path (web intake, real ``InvestigationRunner``, scripted
provider, local HTTP source stub).

ASSUMED (the contract fixes element ids, field names and state names, not the
page's markup or wording); kept in the helpers below so they live in one place:

* ``#coverage-gaps`` lists one row per source as a ``<tr>`` or ``<li>``
  element whose text contains the source name as a separate word
  (``prometheus``, ``jaeger``, ``kubernetes_state``, ... or, for a Run without
  ``data_sources``, the tool name).
* A row shows its D3 state either as the identifier (``all_ok``, ``failed``,
  ...) or the identifier with spaces; ``no_data_only`` and ``not_queried`` may
  instead use the R-8 wording ("returned no data (not evidence of health)",
  "not queried this Run"). Exactly one state is recognisable per row.
* A row shows each non-ok reason code verbatim with its count next to it
  (``SOURCE_UNAVAILABLE ×2``, ``SOURCE_UNAVAILABLE: 2``, ``2 × ...`` ...).
* The B5 count is shown in ``#coverage-gaps`` next to a word starting with
  "duplicate", "repeat" or "re-execut"/"not re-executed".
* ``#collection-truncated`` is present only when the projection says True.
"""

from __future__ import annotations

import dataclasses
import html
import re

from opspilot.investigation.context import INPUT_VERSION_KNOWLEDGE
from opspilot.tools.executor import PROJECTION_REVISION
from opspilot.tools.fixture import fixture_versions
from opspilot.tools.otel_demo import TOOL_SCHEMA_REVISION, otel_demo_versions

# -- page helpers (used by the PostgreSQL module) ---------------------------------

STATES = (
    "all_ok",
    "partial_failure",
    "failed",
    "no_data_only",
    "not_queried",
    "not_authorized",
    "not_integrated",
)
_STATE_TEXT = {state: (state, state.replace("_", " ")) for state in STATES} | {
    "no_data_only": (
        "no_data_only",
        "no data only",
        "returned no data (not evidence of health)",
    ),
    "not_queried": ("not_queried", "not queried"),
}
HEALTHY_WORDING = ("healthy", "正常", "无问题", "no problem", "no issues")


def element(page: str, element_id: str) -> str | None:
    """The outer HTML of the element with ``id=element_id``, or ``None``."""
    match = re.search(
        r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*\bid\s*=\s*[\"']"
        + re.escape(element_id)
        + r"[\"'][^>]*>",
        page,
    )
    if match is None:
        return None
    tag = match.group(1).lower()
    if tag in {"br", "hr", "img", "input", "meta", "link"}:
        return match.group(0)
    depth = 0
    for token in re.finditer(rf"<(/?){tag}\b[^>]*>", page[match.start() :], re.I):
        depth += -1 if token.group(1) else 1
        if depth == 0:
            return page[match.start() : match.start() + token.end()]
    return page[match.start() :]


def text(fragment: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(plain)).strip()


def rows(section: str) -> list[str]:
    """Text of each ``<tr>`` / ``<li>`` in a section (ASSUMED row markup)."""
    parts = re.split(r"(?=<(?:tr|li)\b)", section, flags=re.I)
    return [text(part) for part in parts[1:]]


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")


def row_for(section: str, name: str) -> str:
    found = [row for row in rows(section) if _word(name).search(row)]
    assert found, f"no <tr>/<li> row for {name!r} in #coverage-gaps: {text(section)!r}"
    return found[0]


def states_in(row: str) -> set[str]:
    lowered = row.lower()
    found = set()
    for state, spellings in _STATE_TEXT.items():
        for spelling in spellings:
            if _word(spelling.lower()).search(lowered):
                found.add(state)
    # "failed" is never part of another state's spelling; "not authorized"
    # and "not integrated" contain no other state's words either.
    return found


def state_on_page(section: str, name: str) -> str:
    row = row_for(section, name)
    found = states_in(row)
    assert len(found) == 1, f"row for {name!r} shows states {found}: {row!r}"
    return found.pop()


def reason_count(row: str, reason: str) -> int | None:
    after = re.search(rf"{re.escape(reason)}[^0-9A-Za-z]{{0,6}}(\d+)", row)
    if after:
        return int(after.group(1))
    before = re.search(rf"(\d+)[^0-9A-Za-z]{{0,6}}{re.escape(reason)}", row)
    return int(before.group(1)) if before else None


def duplicate_count(section: str) -> int | None:
    plain = text(section)
    match = re.search(
        r"(?:duplicate|repeat|re-?execut)[^0-9]{0,60}?(\d+)", plain, re.I
    ) or re.search(r"(\d+)[^0-9]{0,40}?(?:duplicate|repeat|re-?execut)", plain, re.I)
    return int(match.group(1)) if match else None


# -- item 17: the model-visible surface does not move -------------------------------

# Printed on main @ e2fcea4 (pre-review section 0, re-printed by this author).
MAIN_PROMPT_REVISION = "prompt-replay-candidate-2f30c2380b23"
MAIN_CONTEXT_POLICY_REVISION = "ctx-ctx-policy-v1-a30e65ea52f4"
MAIN_OTEL_TOOL_SCHEMA_REVISION = "otel-demo-68ed2ca8f5d6"
MAIN_FIXTURE_TOOL_SCHEMA_REVISION = "fixture-4"
MAIN_PROJECTION_REVISION = "m1-01-tool-view-v10"
MAIN_INPUT_VERSION = "opspilot-investigation-input-v5"


def test_item17_no_model_visible_revision_moves():
    assert otel_demo_versions() == {
        "prompt_revision": MAIN_PROMPT_REVISION,
        "context_policy_revision": MAIN_CONTEXT_POLICY_REVISION,
        "tool_schema_revision": MAIN_OTEL_TOOL_SCHEMA_REVISION,
    }
    assert TOOL_SCHEMA_REVISION == MAIN_OTEL_TOOL_SCHEMA_REVISION
    assert fixture_versions() == {
        "prompt_revision": MAIN_PROMPT_REVISION,
        "context_policy_revision": MAIN_CONTEXT_POLICY_REVISION,
        "tool_schema_revision": MAIN_FIXTURE_TOOL_SCHEMA_REVISION,
    }
    assert PROJECTION_REVISION == MAIN_PROJECTION_REVISION
    assert INPUT_VERSION_KNOWLEDGE == MAIN_INPUT_VERSION


# -- R-9: the public projection types -------------------------------------------------


def _fields(cls) -> set[str]:
    assert dataclasses.is_dataclass(cls), cls
    return {f.name for f in dataclasses.fields(cls)}


def test_r9_projection_types_expose_coverage_and_truncation():
    import opspilot.acceptance_investigation as projection

    assert {"coverage"} <= _fields(projection.InvestigationEvidence)
    assert {"collection_truncated"} <= _fields(projection.ReportClaims)
    assert {"run_id", "sources_recorded", "sources", "duplicates_skipped"} <= _fields(
        projection.RunCoverage
    )
    assert {"name", "ok", "no_data", "non_ok", "reasons", "state", "gap"} <= _fields(
        projection.SourceCoverage
    )


# -- self-checks of the page helpers (keep the assumptions honest) ------------------


def test_page_helpers_read_the_assumed_markup():
    page = (
        '<main><section id="coverage-gaps"><h2>Coverage</h2><table>'
        "<tr><th>source</th></tr>"
        "<tr><td>jaeger</td><td>0</td><td>failed</td>"
        "<td>SOURCE_UNAVAILABLE &times;2</td></tr>"
        "<tr><td>prometheus</td><td>1</td><td>all ok</td></tr>"
        "<tr><td>pod_logs</td><td>not integrated</td></tr>"
        "<tr><td>prometheus-x</td><td>returned no data (not evidence of health)</td></tr>"
        "</table><p>Duplicate calls not re-executed: 3</p></section>"
        '<p id="collection-truncated">x</p></main>'
    )
    section = element(page, "coverage-gaps")
    assert section is not None and section.endswith("</section>")
    assert state_on_page(section, "jaeger") == "failed"
    assert state_on_page(section, "prometheus") == "all_ok"
    assert state_on_page(section, "pod_logs") == "not_integrated"
    assert state_on_page(section, "prometheus-x") == "no_data_only"
    assert reason_count(row_for(section, "jaeger"), "SOURCE_UNAVAILABLE") == 2
    assert duplicate_count(section) == 3
    assert element(page, "collection-truncated") is not None
    assert element(page, "missing") is None
