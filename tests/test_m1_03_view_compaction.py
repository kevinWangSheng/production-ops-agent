"""r9 (#199, D47-D50): evidence views over MAX_VIEW_BYTES in the postmortem
input. Pure functions only; the R7 reopening (D52) is in
tests/integration/test_m1_03_view_compaction_postgres.py."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from opspilot.knowledge.generation import (
    MAX_INPUT_BYTES,
    MAX_VIEW_BYTES,
    SYSTEM_PROMPT,
    InputTooLarge,
    ModelOutput,
    Statement,
    _scrub,
    build_input,
    citation_errors,
    project_view,
)
from opspilot.knowledge.store import Watermark, canonical_json

RUN_ID = "11111111-1111-1111-1111-111111111111"
METADATA_KEYS = {
    "evidence_id",
    "run_id",
    "status",
    "adopted",
    "tool",
    "source",
    "target_id",
    "query",
    "window",
    "observed_at",
    "data_as_of",
    "source_start_at",
    "source_end_at",
    "result_count",
    "returned_count",
    "incomplete",
    "truncated",
    "incomplete_reason",
}


def _size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _span(i: int, pad: str = "") -> dict[str, Any]:
    return {
        "trace_id": f"trace-{i:06d}",
        "span_id": f"span-{i:06d}",
        "service": "checkout",
        "operation": "PlaceOrder",
        "duration_ms": i,
        "error": pad,
    }


def _traces_view(spans: int, *, pad: str = "", citable: bool = True) -> dict[str, Any]:
    return {
        "evidence_id": "ignored-the-row-wins",
        "status": "ok",
        "adopted": True,
        "citable_as_fact": citable,
        "tool": "traces_search",
        "source": "jaeger",
        "target_id": "checkout",
        "query": {"service": "checkout", "limit": 20},
        "window": {
            "start": "2026-10-10T10:00:00+00:00",
            "end": "2026-10-10T10:15:00+00:00",
        },
        "observed_at": "2026-10-10T10:15:01+00:00",
        "data_as_of": "2026-10-10T10:15:00+00:00",
        "result_count": spans,
        "returned_count": spans,
        "incomplete": False,
        "truncated": False,
        "content": [_span(i, pad) for i in range(spans)],
        "span_groups": [
            {
                "service": "checkout",
                "operation": "PlaceOrder",
                "rows": spans,
                "errors": 3,
            }
        ],
        "series_note": "note",
        # not in the allowlist: never reaches the model
        "raw_bytes": "x" * 10,
    }


def _logs_view(lines: int, *, pad: str = "x" * 200) -> dict[str, Any]:
    return {
        "status": "ok",
        "adopted": True,
        "citable_as_fact": True,
        "tool": "logs_search",
        "source": "loki",
        "target_id": "checkout",
        "query": {"q": "error"},
        "window": {
            "start": "2026-10-10T10:00:00+00:00",
            "end": "2026-10-10T10:15:00+00:00",
        },
        "result_count": lines,
        "returned_count": lines,
        "content": [f"{i} {pad}" for i in range(lines)],
        "series_note": "n",
    }


def _project(view: dict[str, Any], evidence_id: str = "ev-1") -> dict[str, Any]:
    return project_view(view, evidence_id, RUN_ID, [])


def _legacy(view: dict[str, Any], evidence_id: str = "ev-1") -> dict[str, Any]:
    """The pre-r9 projection, for the byte-for-byte comparison."""
    from opspilot.knowledge.generation import _VIEW_FIELDS

    projected = _scrub({k: view[k] for k in _VIEW_FIELDS if k in view}, [])
    projected["evidence_id"] = evidence_id
    projected["run_id"] = RUN_ID
    return projected


def _raw(*views: dict[str, Any]) -> dict[str, Any]:
    now = datetime(2026, 10, 10, 10, 20, tzinfo=UTC)
    return {
        "incident": {
            "incident_id": uuid4(),
            "control_generation": 0,
            "observation_generation": 1,
            "intake_key": "i",
            "lifecycle": "resolved",
            "state": "completed",
            "mode": "automatic",
            "target_id": uuid4(),
            "resource_uid": "r",
            "current_run_id": None,
            "created_at": None,
            "conclusion": {},
        },
        "watermark": Watermark(1, 1, uuid4(), uuid4(), 1, uuid4(), 1, "h"),
        "observation": {
            "session": {
                "state": "completed",
                "session_id": uuid4(),
                "authorized_by": "x",
                "authorized_at": None,
                "max_samples": 1,
                "sample_interval_seconds": 1,
                "sustained_window_seconds": 1,
                "health_profile_revision": None,
                "adopted_count": 0,
                "healthy_since": None,
            },
            "ending": {
                "ending_id": uuid4(),
                "ended_reason": "x",
                "recorded_at": None,
                "lifecycle_before": "x",
                "lifecycle_after": "x",
                "transition": "x",
            },
            "samples": [],
            "readings": [],
        },
        "evidence": [
            {
                "evidence_id": f"ev-{n}",
                "run_id": RUN_ID,
                "status": view.get("status", "ok"),
                "adopted": True,
                "view": view,
                "observed_at": now,
            }
            for n, view in enumerate(views, start=1)
        ],
        "controls": [],
        "inputs": [],
        "runs": [],
        "postmortem": None,
        "pending_regeneration_version": None,
        "return_reason": None,
        "published_entries": {},
    }


def _fact(*ids: str) -> ModelOutput:
    return ModelOutput(
        statements=(Statement("f", "findings", "fact", "b", tuple(ids), False),),
        proposals=(),
        disputes=(),
    )


# --- views within the limit -----------------------------------------------------


@pytest.mark.parametrize("view", [_traces_view(20), _logs_view(10)])
def test_view_within_the_limit_projects_byte_for_byte_as_before(view):
    projected = _project(view)
    assert _size(projected) <= MAX_VIEW_BYTES
    assert canonical_json(projected) == canonical_json(_legacy(view))
    assert "projection" not in projected


def test_catalog_of_a_view_within_the_limit_has_no_projection():
    built = build_input(_raw(_traces_view(20)), secrets=[])
    (entry,) = built.catalog
    assert "projection" not in entry and entry["citable_as_fact"] is True
    assert built.payload["evidence_views"][0] == _legacy(_traces_view(20))


# --- D47: traces views compacted to span_groups -----------------------------------


def test_oversized_traces_view_drops_content_and_keeps_the_rest():
    view = _traces_view(400)
    assert _size(_legacy(view)) > MAX_VIEW_BYTES
    projected = _project(view)
    legacy = _legacy(view)
    assert projected["projection"] == "span_groups_only"
    assert projected["omitted_content_items"] == 400
    assert "content" not in projected and "raw_bytes" not in projected
    assert {
        k: v
        for k, v in projected.items()
        if k not in ("projection", "omitted_content_items")
    } == {k: v for k, v in legacy.items() if k != "content"}
    assert projected["citable_as_fact"] is True
    assert _size(projected) <= MAX_VIEW_BYTES


def test_non_list_content_is_counted_as_null():
    view = _traces_view(0)
    view["content"] = "y" * (MAX_VIEW_BYTES + 1)
    projected = _project(view)
    assert projected["projection"] == "span_groups_only"
    assert projected["omitted_content_items"] is None


def test_traces_view_without_a_span_groups_list_falls_back_to_metadata():
    view = _traces_view(400)
    view["span_groups"] = None
    projected = _project(view)
    assert projected["projection"] == "metadata_only"


def test_compaction_keeps_the_view_citable_as_given():
    projected = _project(_traces_view(400, citable=False))
    assert projected["projection"] == "span_groups_only"
    assert projected["citable_as_fact"] is False


# --- D48: everything else over the limit is metadata only --------------------------


def test_oversized_other_view_is_metadata_only_and_never_citable():
    view = _logs_view(400)
    view["freshness_seconds"] = 5
    view["lookback_seconds"] = 900
    projected = _project(view)
    assert projected["projection"] == "metadata_only"
    assert projected["citable_as_fact"] is False
    assert set(projected) - {"citable_as_fact", "projection"} <= METADATA_KEYS
    assert projected["evidence_id"] == "ev-1" and projected["run_id"] == RUN_ID
    assert projected["query"] == view["query"] and projected["window"] == view["window"]
    assert projected["result_count"] == 400
    for dropped in ("content", "series_note", "freshness_seconds", "lookback_seconds"):
        assert dropped not in projected


def test_traces_view_whose_span_groups_alone_are_over_the_limit_is_metadata_only():
    view = _traces_view(2)
    view["span_groups"] = [
        {"service": f"s{i}", "operation": "o" * 200, "rows": 1} for i in range(200)
    ]
    projected = _project(view)
    assert projected["projection"] == "metadata_only"
    assert "span_groups" not in projected and "omitted_content_items" not in projected


def test_metadata_still_over_the_limit_refuses_the_input():
    view = _logs_view(1)
    view["query"] = {"q": "z" * (MAX_VIEW_BYTES + 1)}
    with pytest.raises(InputTooLarge, match="MAX_VIEW_BYTES"):
        _project(view)
    with pytest.raises(InputTooLarge, match="MAX_VIEW_BYTES"):
        build_input(_raw(view), secrets=[])


def test_metadata_is_scrubbed_like_the_full_view():
    view = _logs_view(400)
    view["query"] = {"q": "password=hunter2hunter2", "api_key": "dropped-by-name"}
    projected = _project(view)
    assert projected["projection"] == "metadata_only"
    assert "hunter2hunter2" not in canonical_json(projected)
    assert "api_key" not in projected["query"]


# --- byte boundaries ------------------------------------------------------------------


def _padded_to(target: int, char: str) -> dict[str, Any]:
    """A logs view whose legacy projection is exactly ``target`` bytes:
    ``char`` (1 to 4 UTF-8 bytes) fills the note, ASCII the remainder."""
    view = _logs_view(1, pad="")
    view["series_note"] = ""
    room = target - _size(_legacy(view))
    width = len(char.encode("utf-8"))
    view["series_note"] = char * (room // width) + "a" * (room % width)
    assert _size(_legacy(view)) == target
    return view


@pytest.mark.parametrize("char", ["a", "é", "日", "😀"])
def test_exactly_the_limit_is_within_it(char):
    view = _padded_to(MAX_VIEW_BYTES, char)
    projected = _project(view)
    assert "projection" not in projected
    assert canonical_json(projected) == canonical_json(_legacy(view))


def test_one_byte_over_the_limit_is_reduced():
    view = _padded_to(MAX_VIEW_BYTES, "a")
    view["series_note"] += "a"
    assert _project(view)["projection"] == "metadata_only"


def test_the_limit_counts_utf8_bytes_not_characters():
    view = _logs_view(1, pad="")
    # under the limit in characters, over it in bytes
    view["series_note"] = "é" * (MAX_VIEW_BYTES // 2 + 10)
    assert len(canonical_json(_legacy(view))) < MAX_VIEW_BYTES
    assert _size(_legacy(view)) > MAX_VIEW_BYTES
    assert _project(view)["projection"] == "metadata_only"


def test_compact_traces_view_exactly_at_the_limit_is_used():
    view = _traces_view(400)
    view["series_note"] = ""
    compact_base = _size(_project(view))
    view["series_note"] = "a" * (MAX_VIEW_BYTES - compact_base)
    projected = _project(view)
    assert projected["projection"] == "span_groups_only"
    assert _size(projected) == MAX_VIEW_BYTES
    view["series_note"] += "a"
    assert _project(view)["projection"] == "metadata_only"


# --- D49: catalog and citations -------------------------------------------------------


def test_catalog_carries_the_projection_and_citability_of_the_view():
    built = build_input(
        _raw(_traces_view(20), _traces_view(400), _logs_view(400)), secrets=[]
    )
    views = {v["evidence_id"]: v for v in built.payload["evidence_views"]}
    catalog = {e["evidence_id"]: e for e in built.catalog}
    assert "projection" not in catalog["ev-1"]
    assert catalog["ev-2"]["projection"] == "span_groups_only"
    assert catalog["ev-3"]["projection"] == "metadata_only"
    assert catalog["ev-1"]["citable_as_fact"] is True
    assert catalog["ev-2"]["citable_as_fact"] is True
    assert catalog["ev-3"]["citable_as_fact"] is False
    for evidence_id in ("ev-2", "ev-3"):
        assert views[evidence_id]["projection"] == catalog[evidence_id]["projection"]
        assert (
            views[evidence_id]["citable_as_fact"]
            == catalog[evidence_id]["citable_as_fact"]
        )
    assert built.payload["evidence_catalog"] == list(built.catalog)


def test_fact_citing_only_metadata_only_evidence_fails_its_citation_check():
    built = build_input(_raw(_traces_view(400), _logs_view(400)), secrets=[])
    assert citation_errors(_fact("ev-2"), built.catalog) == {
        "f": ["NOT_CITABLE_AS_FACT"]
    }
    assert citation_errors(_fact("ev-1"), built.catalog) == {}
    assert citation_errors(_fact("ev-1", "ev-2"), built.catalog) == {}


def test_prompt_explains_both_projections():
    assert "span_groups_only" in SYSTEM_PROMPT
    assert "metadata_only" in SYSTEM_PROMPT
    assert "omitted_content_items" in SYSTEM_PROMPT


# --- D50 and determinism -----------------------------------------------------------------


def test_total_input_limit_still_applies_after_compaction():
    # each traces view compacts to about 30 KiB of span_groups
    view = _traces_view(400)
    view["span_groups"] = [
        {"service": f"s{i}", "operation": "o" * 100, "rows": 1} for i in range(220)
    ]
    assert _project(view)["projection"] == "span_groups_only"
    views = [copy.deepcopy(view) for _ in range(10)]
    assert sum(_size(_project(v)) for v in views) > MAX_INPUT_BYTES
    with pytest.raises(InputTooLarge, match="MAX_INPUT_BYTES"):
        build_input(_raw(*views), secrets=[])


def test_same_rows_give_the_same_model_input_bytes_and_hash():
    raw = _raw(_traces_view(20), _traces_view(400), _logs_view(400))
    first = build_input(raw, secrets=[])
    second = build_input(copy.deepcopy(raw), secrets=[])
    assert first.payload_text == second.payload_text
    assert first.payload_sha256 == second.payload_sha256
    assert first.catalog == second.catalog
