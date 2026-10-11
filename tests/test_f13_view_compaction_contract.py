"""Independent #199 contract r9: D47–D51 through public pure functions.

The allowlist below is frozen from main d8213f0, not imported from the
implementation: unchanged views must retain that exact canonical projection.
"""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

import pytest

from opspilot.knowledge import contract
from opspilot.knowledge.generation import (
    MAX_INPUT_BYTES,
    MAX_VIEW_BYTES,
    InputTooLarge,
    build_input,
    citation_errors,
    messages,
    parse_model_output,
)
from opspilot.knowledge.store import Watermark, canonical_json

T0 = datetime(2026, 10, 11, tzinfo=timezone.utc)
IDENT = UUID("00000000-0000-0000-0000-000000000001")
BASELINE_FIELDS = (
    "evidence_id",
    "status",
    "adopted",
    "citable_as_fact",
    "tool",
    "source",
    "target_id",
    "query",
    "window",
    "observed_at",
    "data_as_of",
    "source_start_at",
    "source_end_at",
    "freshness_seconds",
    "lookback_seconds",
    "result_count",
    "returned_count",
    "incomplete",
    "truncated",
    "content",
    "series_note",
    "span_groups",
    "incomplete_reason",
)
METADATA_FIELDS = (
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
)
GROUPS = [
    {
        "service": "checkout",
        "operation": "GET /checkout",
        "rows": 2,
        "status": {"http.status_code=500": 2},
        "error_rows": 2,
        "duration_us_min": 100,
        "duration_us_max": 200,
    }
]


def _view(**updates):
    view = {
        "evidence_id": "untrusted-id",
        "run_id": "untrusted-run",
        "status": "ok",
        "adopted": True,
        "citable_as_fact": True,
        "tool": "traces_search",
        "source": "jaeger",
        "target_id": "checkout",
        "query": {"service": "checkout", "limit": 20},
        "window": {"start": T0.isoformat(), "end": T0.isoformat()},
        "observed_at": T0.isoformat(),
        "data_as_of": T0.isoformat(),
        "source_start_at": T0.isoformat(),
        "source_end_at": T0.isoformat(),
        "freshness_seconds": 0,
        "lookback_seconds": 300,
        "result_count": 2,
        "returned_count": 2,
        "incomplete": False,
        "truncated": False,
        "content": [{"span_id": "a"}, {"span_id": "b"}],
        "series_note": "visible rows",
        "span_groups": deepcopy(GROUPS),
        "incomplete_reason": None,
        "raw_private": "not visible",
        "projection": "upstream-projection",
    }
    view.update(updates)
    return view


def _baseline(view, evidence_id="ev", run_id="run"):
    projected = {k: deepcopy(view[k]) for k in BASELINE_FIELDS if k in view}
    projected.update(evidence_id=evidence_id, run_id=run_id)
    return projected


def _size(value):
    return len(canonical_json(value).encode("utf-8"))


def _raw(*views):
    return {
        "incident": {
            "incident_id": IDENT,
            "current_run_id": None,
            "intake_key": "test",
            "target_id": None,
            "resource_uid": "checkout",
            "state": "completed",
            "lifecycle": "resolved",
            "mode": "incident",
            "created_at": T0,
            "control_generation": 0,
            "observation_generation": 0,
            "conclusion": None,
        },
        "watermark": Watermark(0, 0, IDENT, IDENT, 0, None, 0, "a" * 64),
        "observation": {
            "session": {
                "health_profile_revision": "v1",
                "authorized_by": "test",
                "authorized_at": T0,
                "state": "completed",
                "adopted_count": 0,
                "max_samples": 1,
                "sample_interval_seconds": 1,
                "sustained_window_seconds": 1,
                "healthy_since": None,
            },
            "ending": {
                "ended_reason": "authority_revoked",
                "transition": None,
                "lifecycle_before": "observing_recovery",
                "lifecycle_after": "resolved",
                "recorded_at": T0,
            },
            "samples": [],
            "readings": [],
        },
        "evidence": [
            {
                "view": v,
                "evidence_id": "ev" if i == 0 else f"ev-{i}",
                "run_id": "run",
                "adopted": True,
                "status": "ok",
                "observed_at": T0,
            }
            for i, v in enumerate(views)
        ],
        "controls": [],
        "inputs": [],
        "runs": [],
        "postmortem": None,
        "pending_regeneration_version": None,
        "return_reason": None,
        "published_entries": {},
    }


def _build(view):
    return build_input(_raw(view), secrets=[])


@pytest.mark.parametrize("tool", ["traces_search", "metrics_query"])
def test_d47_small_view_matches_frozen_main_projection_byte_for_byte(tool):
    view = _view(tool=tool)
    actual = _build(view).payload["evidence_views"][0]
    assert canonical_json(actual).encode("utf-8") == canonical_json(
        _baseline(view)
    ).encode("utf-8")


def test_d47_limit_is_applied_after_allowlist_and_redaction():
    view = _view(raw_private="x" * (MAX_VIEW_BYTES * 2), content=["known-value" * 4000])
    actual = build_input(_raw(view), secrets=["known-value" * 4000]).payload[
        "evidence_views"
    ][0]
    expected = _baseline(view)
    expected["content"] = ["[REDACTED]"]
    assert canonical_json(actual) == canonical_json(expected)


@pytest.mark.parametrize("citable", [True, False])
@pytest.mark.parametrize(
    "content", [["x" * 40000, "second"], "x" * 40000], ids=["list", "string"]
)
def test_d47_oversized_traces_keep_groups_and_original_citability(citable, content):
    view = _view(content=content, citable_as_fact=citable)
    assert _size(_baseline(view)) > MAX_VIEW_BYTES
    actual = _build(view).payload["evidence_views"][0]
    expected = _baseline(view)
    expected.pop("content", None)
    expected.update(
        projection="span_groups_only",
        omitted_content_items=2 if isinstance(content, list) else None,
    )
    assert actual == expected
    assert _size(actual) <= MAX_VIEW_BYTES


@pytest.mark.parametrize(
    "case",
    [
        "non_traces",
        "large_groups",
        "missing_groups",
        "non_list_groups",
        "absent_content",
        "sparse_metadata",
    ],
)
def test_d48_fallback_has_exact_metadata_allowlist(case):
    view = _view(content=["x" * 40000])
    if case == "non_traces":
        view["tool"] = "logs_search"
    elif case == "large_groups":
        view["span_groups"] = [{"operation": "x" * 40000}]
    elif case == "missing_groups":
        view.pop("span_groups")
    elif case == "non_list_groups":
        view["span_groups"] = {"not": "a list"}
    elif case == "absent_content":
        view.pop("content")
        view["series_note"] = "x" * 40000
    else:
        view = {"tool": "logs_search", "content": ["x" * 40000]}
    actual = _build(view).payload["evidence_views"][0]
    baseline = _baseline(view)
    expected = {k: baseline[k] for k in METADATA_FIELDS if k in baseline}
    expected.update(citable_as_fact=False, projection="metadata_only")
    assert actual == expected
    assert _size(actual) <= MAX_VIEW_BYTES


def test_d48_metadata_still_too_large_names_view_limit():
    with pytest.raises(InputTooLarge) as caught:
        _build(_view(query={"service": "x" * 40000}, content=["x" * 40000]))
    assert caught.value.limit == "MAX_VIEW_BYTES"
    assert str(caught.value) == "MAX_VIEW_BYTES"


@pytest.mark.parametrize(
    "tool,projection,citable",
    [
        ("traces_search", "span_groups_only", True),
        ("traces_search", "span_groups_only", False),
        ("logs_search", "metadata_only", False),
    ],
)
def test_d49_catalog_and_fact_citations_follow_projected_view(
    tool, projection, citable
):
    built = _build(
        _view(
            tool=tool,
            content=["x" * 40000],
            citable_as_fact=citable if tool == "traces_search" else True,
        )
    )
    view = built.payload["evidence_views"][0]
    entry = next(e for e in built.catalog if e["evidence_id"] == "ev")
    assert entry["citable_as_fact"] is view["citable_as_fact"] is citable
    assert entry["projection"] == view["projection"] == projection
    assert built.payload["evidence_catalog"] == list(built.catalog)
    output = parse_model_output(
        canonical_json(
            {
                "narrative_sections": [
                    {
                        "key": "impact",
                        "section": "impact_summary",
                        "body": "Checkout errors observed.",
                        "evidence_ids": ["ev"],
                    }
                ],
                "conclusions": [
                    {
                        "key": "finding",
                        "section": "findings",
                        "claim": "fact",
                        "body": "Checkout group contains errors.",
                        "evidence_ids": ["ev"],
                    }
                ],
                "proposals": [],
                "disputes": [],
            }
        )
    )
    assert citation_errors(output, built.catalog) == (
        {}
        if citable
        else {
            "impact": ["NOT_CITABLE_AS_FACT"],
            "finding": ["NOT_CITABLE_AS_FACT"],
        }
    )


def test_d50_total_limit_is_checked_after_successful_compaction():
    assert MAX_VIEW_BYTES == 32 * 1024
    assert MAX_INPUT_BYTES == 256 * 1024
    view = _view(content=["x" * 40000], span_groups=[{"operation": "x" * 29000}])
    compact = _build(view).payload["evidence_views"][0]
    assert compact["projection"] == "span_groups_only"
    assert _size(compact) < MAX_VIEW_BYTES
    # Nine individually legal compacted views exceed the total limit.
    with pytest.raises(InputTooLarge) as caught:
        build_input(_raw(*(deepcopy(view) for _ in range(9))), secrets=[])
    assert caught.value.limit == "MAX_INPUT_BYTES"


@pytest.mark.parametrize("character", ["x", "界"])
@pytest.mark.parametrize(
    "extra_byte", [False, True], ids=["exact_limit", "one_byte_over"]
)
def test_d47_exact_32k_boundary_and_utf8_byte_count(character, extra_byte):
    view = _view(content="")
    room = MAX_VIEW_BYTES - _size(_baseline(view))
    width = len(character.encode("utf-8"))
    count, remainder = divmod(room, width)
    view["content"] = character * count + "x" * remainder
    assert _size(_baseline(view)) == MAX_VIEW_BYTES
    if extra_byte:
        view["content"] += "x"
        assert _size(_baseline(view)) == MAX_VIEW_BYTES + 1
        assert (
            _build(view).payload["evidence_views"][0]["projection"]
            == "span_groups_only"
        )
    else:
        actual = _build(view).payload["evidence_views"][0]
        assert canonical_json(actual) == canonical_json(_baseline(view))


@pytest.mark.parametrize(
    "name,expected",
    [
        ("POSTMORTEM_INPUT_POLICY_VERSION", "f13-postmortem-input-v3"),
        ("POSTMORTEM_PROMPT_VERSION", "f13-postmortem-prompt-v4"),
        ("POSTMORTEM_OUTPUT_SCHEMA_VERSION", "f13-postmortem-output-v2"),
        ("POSTMORTEM_CONTENT_SCHEMA_VERSION", "f13-postmortem-content-v1"),
    ],
)
def test_d51_versions(name, expected):
    assert getattr(contract, name) == expected


def test_d51_prompt_names_both_projection_rules():
    prompt = messages(_build(_view()), secrets=[])[0]["content"]
    assert "span_groups_only" in prompt
    assert "metadata_only" in prompt


@pytest.mark.parametrize("tool", ["traces_search", "logs_search"])
def test_compaction_is_deterministic_and_does_not_mutate_input(tool):
    raw = _raw(_view(tool=tool, content=["x" * 40000]))
    original = deepcopy(raw)
    first = build_input(raw, secrets=[])
    second = build_input(raw, secrets=[])
    assert first == second
    assert first.payload_text.encode("utf-8") == second.payload_text.encode("utf-8")
    assert first.payload_sha256 == second.payload_sha256
    assert raw == original
