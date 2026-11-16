"""Implementation-side tests for #646 PR-A (report ``root_cause``, v3).

The contract/acceptance tests are written independently; these cover the
implementer's own choices: v2/v3 coexistence, the delivered-id-only citation
check, retry feedback wording, and that summaries frozen before v3 still pass
the eval summary loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import storage
from opspilot.evaluation.codec import canonical_bytes, parse
from opspilot.investigation.reports import (
    REPORT_CONTRACT,
    REPORT_SCHEMA_VERSION,
    DeliveredView,
    parse_report,
    report_retry_feedback,
    unsupported_citations,
)

ROOT = Path(__file__).resolve().parents[2]
FROZEN = sorted((ROOT / "docs/evidence/eval").rglob("summary.json"))


def _report(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "m0-report-v3",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": "cart failed.",
        "claims": [
            {
                "kind": "fact",
                "text": "cart errors rose.",
                "evidence_ids": ["ev-1"],
                "target_refs": ["t-1"],
                "time_scope_ref": "tp-1",
            }
        ],
        "gaps": [],
        "next_steps": [],
    }
    base.update(extra)
    return base


def _parse(payload: dict[str, Any]) -> Any:
    return parse_report(json.dumps(payload), finish_reason="stop")


VIEW = DeliveredView(
    evidence_id="ev-1",
    target_ids=frozenset({"t-1"}),
    status="ok",
    time_scope_refs=frozenset({"tp-1"}),
    citable_as_fact=True,
)
CHECK = {
    "views": [VIEW],
    "authorized_targets": frozenset({"t-1"}),
    "time_policy_ids": ("tp-1",),
}
ROOT_CAUSE = {
    "kind": "Deployment",
    "namespace": "shop",
    "name": "cart",
    "evidence_ids": ["ev-1"],
}


def test_contract_asks_for_v3_and_describes_root_cause() -> None:
    assert REPORT_SCHEMA_VERSION == "m0-report-v3"
    assert "schema_version must be m0-report-v3" in REPORT_CONTRACT
    assert '"root_cause":null' in REPORT_CONTRACT


@pytest.mark.parametrize("version", ["m0-report-v2", "m0-report-v3"])
def test_both_versions_parse_without_root_cause(version: str) -> None:
    report, reason = _parse(_report(schema_version=version))
    assert reason == "" and report is not None
    assert report.root_cause is None
    assert "root_cause" not in report.model_fields_set


def test_explicit_null_is_distinguishable_from_absent() -> None:
    report, _ = _parse(_report(root_cause=None))
    assert report is not None and report.root_cause is None
    assert "root_cause" in report.model_fields_set


def test_namespace_may_be_null_or_omitted() -> None:
    for rc in (
        {**ROOT_CAUSE, "namespace": None},
        {k: v for k, v in ROOT_CAUSE.items() if k != "namespace"},
        {**ROOT_CAUSE, "namespace": ""},
    ):
        report, reason = _parse(_report(root_cause=rc))
        assert reason == "" and report is not None
        assert report.root_cause is not None
        assert not report.root_cause.namespace


@pytest.mark.parametrize(
    "bad",
    [
        {k: v for k, v in ROOT_CAUSE.items() if k != "kind"},
        {k: v for k, v in ROOT_CAUSE.items() if k != "name"},
        {**ROOT_CAUSE, "evidence_ids": []},
        {**ROOT_CAUSE, "extra": "x"},
        {**ROOT_CAUSE, "name": ""},
    ],
)
def test_malformed_root_cause_is_report_invalid(bad: dict[str, Any]) -> None:
    assert _parse(_report(root_cause=bad)) == (None, "REPORT_INVALID")


def test_inconclusive_with_root_cause_is_not_rejected() -> None:
    report, reason = _parse(_report(conclusion="inconclusive", root_cause=ROOT_CAUSE))
    assert reason == "" and report is not None


def test_root_cause_citation_checks_delivered_ids_only() -> None:
    ok, _ = _parse(_report(root_cause=ROOT_CAUSE))
    assert not unsupported_citations(ok, **CHECK)
    # A non-ok, non-citable view still satisfies the delivered-id check.
    weak = DeliveredView(evidence_id="ev-2", target_ids=frozenset(), status="no_data")
    rc, _ = _parse(_report(root_cause={**ROOT_CAUSE, "evidence_ids": ["ev-2"]}))
    assert not unsupported_citations(rc, **{**CHECK, "views": [VIEW, weak]})
    bad, _ = _parse(_report(root_cause={**ROOT_CAUSE, "evidence_ids": ["ev-x"]}))
    assert unsupported_citations(bad, **CHECK)
    feedback = report_retry_feedback("REPORT_INVALID", report=bad, **CHECK)
    assert "root_cause: unknown_evidence_id (evidence_id: ev-x)." in feedback
    assert "claim 0" not in feedback


def test_schema_feedback_names_root_cause_field() -> None:
    content = json.dumps(_report(root_cause={**ROOT_CAUSE, "evidence_ids": []}))
    feedback = report_retry_feedback(
        "REPORT_INVALID", report=None, content=content, **CHECK
    )
    assert "root_cause.evidence_ids: schema_error" in feedback


def _relaxed_resolver(doc: dict[str, Any]) -> dict[str, bytes]:
    """Delivery bytes rebuilt from the summary itself (raw refs are not in git)."""
    by_path: dict[str, bytes] = {}
    for key, ref in doc["capture"]["delivery_refs"].items():
        by_path[ref["path"]] = canonical_bytes(doc["capture"]["delivered"][key])
    return by_path


@pytest.mark.skipif(not FROZEN, reason="no frozen eval summaries")
def test_frozen_pre_v3_summaries_still_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the hash check on unavailable raw artifacts is relaxed; every
    # report-related check in ``_load_summary`` runs unchanged.
    monkeypatch.setattr(storage, "_resolve", lambda ref, resolve: resolve(ref))
    loaded = 0
    for path in FROZEN:
        raw = path.read_bytes()
        doc = parse(raw)
        saved = doc["report_parsed"]
        assert saved is None or "root_cause" not in saved
        blobs = _relaxed_resolver(doc)
        capture = storage.decode(storage.EvaluationCapture, doc["capture"])
        storage._load_summary(
            raw, capture.manifest, lambda ref: blobs.get(ref.path, b"")
        )
        loaded += 1
    assert loaded == len(FROZEN)


@pytest.mark.skipif(not FROZEN, reason="no frozen eval summaries")
def test_frozen_summary_with_tampered_root_cause_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "_resolve", lambda ref, resolve: resolve(ref))
    path = next(p for p in FROZEN if parse(p.read_bytes())["report_parsed"])
    doc = parse(path.read_bytes())
    doc["report_parsed"]["root_cause"] = ROOT_CAUSE
    blobs = _relaxed_resolver(doc)
    capture = storage.decode(storage.EvaluationCapture, doc["capture"])
    with pytest.raises(storage.StorageError):
        storage._load_summary(
            canonical_bytes(doc), capture.manifest, lambda ref: blobs.get(ref.path, b"")
        )
