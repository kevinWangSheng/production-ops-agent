"""Holmes ``summarize`` recomputes Q11 from stored evidence (F1 ruling
2026-11-05, S2; follow-up to #493).

Attempts are written by ``attempt_summary``, the producer of the real exam
evidence: ``proxy_requests_holmes`` keyed ``"METHOD uri status"`` and the raw
``out/holmes.json`` with its tool calls. The exam's stored ``read_only`` was
computed before #493, so these tests overwrite it to model that.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_baseline import dataset as ds
from tests.test_s7_holmes_dataset_impl import DATASET, fake_output, write_attempt

SID = "dv2-2b214b"
#: Shapes seen in the exam evidence (~/.agent/eval-probes/f1-exam-2026-11-05).
EXAM_LINES = (
    "t holmes GET /api/v1/label/__name__/values 200 1 2 0.1",
    "t holmes GET /api/v1/series 200 1 2 0.1",
    "t holmes POST /api/v1/query 200 1 2 0.1",
    "t holmes POST /api/v1/query 200 1 2 0.1",
    "t holmes POST /api/v1/query_range 200 1 2 0.1",
)
JAEGER_GET = {
    "tool_name": "jaeger_request",
    "description": "HTTP GET http://f14-lab-relay-i2:16686/jaeger/ui/api/services",
    "result": {"status": "success"},
}


def _attempt(
    root: Path,
    *,
    lines: tuple[str, ...] = EXAM_LINES,
    output: dict[str, Any] | None = None,
    recorded: bool = False,
    **summary_edits: Any,
) -> Path:
    write_attempt(
        root,
        SID,
        "a1",
        100.0,
        lines=lines,
        output=output or fake_output(tool_calls=[JAEGER_GET]),
    )
    path = root / "a1" / ds.SUMMARY_FILE
    summary = json.loads(path.read_text())
    summary["checks"]["read_only"] = recorded
    for key, value in summary_edits.items():
        if value is None:
            summary.pop(key, None)
        else:
            summary[key] = value
    path.write_text(json.dumps(summary))
    return root


def _slot(root: Path) -> dict[str, Any]:
    doc = ds.summarize(DATASET, root, n=3)
    (slot,) = [s for s in doc["slots"] if s["status"] == "ran"]
    return {**slot, "_doc": doc}


def test_stored_false_with_prometheus_post_queries_is_recomputed_read_only(
    tmp_path: Path,
) -> None:
    _attempt(tmp_path, recorded=False)
    stored = json.loads((tmp_path / "a1" / ds.SUMMARY_FILE).read_text())
    assert stored["proxy_requests_holmes"]["POST /api/v1/query 200"] == 2
    slot = _slot(tmp_path)
    assert slot["checks"]["read_only"] is True
    assert slot["checks"]["read_only_recorded"] is False
    assert slot["read_only"] is True
    doc = slot["_doc"]
    group = slot["group"]
    assert doc["totals"][group]["read_only"] == 1
    inputs = ds.judge_inputs(doc, tmp_path, tmp_path / "judge.json")
    assert [a["read_only"] for a in inputs["attempts"]] == [True]
    # stored evidence is not rewritten
    assert json.loads((tmp_path / "a1" / ds.SUMMARY_FILE).read_text()) == stored


@pytest.mark.parametrize(
    "line",
    [
        "t holmes POST /api/v1/admin/tsdb/delete_series 200 1 2 0.1",
        "t holmes POST /api/v1/query_exemplars 200 1 2 0.1",
        "t holmes PUT /api/v1/query 403 1 2 0.1",
        "t holmes DELETE /api/v1/series 403 1 2 0.1",
    ],
)
def test_a_stored_write_request_is_not_read_only_even_if_recorded_true(
    tmp_path: Path, line: str
) -> None:
    _attempt(tmp_path, lines=(*EXAM_LINES, line), recorded=True)
    slot = _slot(tmp_path)
    assert slot["checks"]["read_only"] is False
    assert slot["checks"]["read_only_recorded"] is True


def test_get_only_attempt_stays_read_only(tmp_path: Path) -> None:
    _attempt(tmp_path, lines=EXAM_LINES[:2], recorded=True)
    assert _slot(tmp_path)["checks"]["read_only"] is True


def test_jaeger_tool_call_rule_is_unchanged(tmp_path: Path) -> None:
    post = {**JAEGER_GET, "description": "HTTP POST http://x/api/services"}
    _attempt(tmp_path, output=fake_output(tool_calls=[JAEGER_GET, post]))
    assert _slot(tmp_path)["checks"]["read_only"] is False


@pytest.mark.parametrize(
    "requests", [None, ["GET /api/v1/query 200"], {"POST": 1}, {"": 1}]
)
def test_missing_or_unparseable_request_counts_fail_closed(
    tmp_path: Path, requests: Any
) -> None:
    edit: dict[str, Any] = {"proxy_requests_holmes": requests}
    _attempt(tmp_path, recorded=True, **edit)
    assert _slot(tmp_path)["checks"]["read_only"] is False


def _drop_raw(root: Path) -> None:
    path = root / "a1" / ds.SUMMARY_FILE
    summary = json.loads(path.read_text())
    summary["raw"] = {"file": None, "sha256": None}
    path.write_text(json.dumps(summary))
    (root / "a1" / ds.RAW_FILE).unlink()


def test_no_raw_output_passes_only_without_recorded_tool_calls(
    tmp_path: Path,
) -> None:
    # exam shape of a failed start: no raw file, no tool rows, GET requests
    _attempt(tmp_path / "none", lines=EXAM_LINES[:1], recorded=True, tool_calls=[])
    _drop_raw(tmp_path / "none")
    assert _slot(tmp_path / "none")["checks"]["read_only"] is True
    _attempt(tmp_path / "calls", recorded=True)
    _drop_raw(tmp_path / "calls")
    assert _slot(tmp_path / "calls")["checks"]["read_only"] is False


@pytest.mark.parametrize("raw", [b"{not json", b"null", b"[]", b'"text"'])
def test_unparseable_or_non_object_raw_output_fails_closed(
    tmp_path: Path, raw: bytes
) -> None:
    # tool_calls [] would pass if a present raw file were taken as absent
    _attempt(tmp_path, recorded=True, tool_calls=[])
    (tmp_path / "a1" / ds.RAW_FILE).write_bytes(raw)
    path = tmp_path / "a1" / ds.SUMMARY_FILE
    summary = json.loads(path.read_text())
    summary["raw"]["sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(summary))
    assert _slot(tmp_path)["checks"]["read_only"] is False


def test_attempt_time_and_stored_checks_share_one_rule() -> None:
    output = fake_output(tool_calls=[JAEGER_GET])
    for lines in (
        EXAM_LINES,
        (*EXAM_LINES, "t holmes POST /api/v1/admin/tsdb/snapshot 200 1 2 0.1"),
        ("t holmes PATCH /api/v1/query 403 1 2 0.1",),
    ):
        counts = {" ".join(line.split()[2:5]): 1 for line in lines}
        stored = {"proxy_requests_holmes": counts, "tool_calls": []}
        assert ds.stored_read_only(stored, output) is ds.read_only(output, lines)
