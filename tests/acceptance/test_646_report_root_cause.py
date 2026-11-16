"""Independent contract tests for the structured report root cause (#646).

These tests use only the report parser/validator and the public evaluation
storage entry point.  All report and evidence values are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation.codec import canonical_bytes
from opspilot.evaluation.rounds import DimensionValue, Manifest
from opspilot.evaluation.storage import load_summary
from opspilot.investigation.reports import DeliveredView, parse_report, validated_report

DELIVERED_ID = "ev-a"


def _root_cause(
    *,
    kind: str = "service",
    name: str = "payment",
    namespace: str | None = "ops",
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "evidence_ids": evidence_ids if evidence_ids is not None else [DELIVERED_ID],
    }
    return value


def _report(
    *,
    schema_version: str = "m0-report-v3",
    root_cause: object = "present",
    conclusion: str = "partial",
    assessment_status: str = "completed",
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "assessment_status": assessment_status,
        "conclusion": conclusion,
        "summary": "Synthetic report for a bounded contract test.",
        "claims": [],
        "gaps": gaps or [],
        "next_steps": [],
    }
    if root_cause != "omitted":
        payload["root_cause"] = root_cause
    return payload


def _validated(
    payload: dict[str, Any], *, delivered: tuple[str, ...] = (DELIVERED_ID,)
):
    views = [
        DeliveredView(
            evidence_id=evidence_id,
            target_ids=frozenset(),
            status="ok",
        )
        for evidence_id in delivered
    ]
    return validated_report(
        json.dumps(payload),
        finish_reason="stop",
        delivered=views,
        evidence_context=None,
        authorized_targets=frozenset(),
    )


@pytest.mark.parametrize(
    ("payload", "feedback_token"),
    [
        (
            _report(root_cause={"name": "payment", "evidence_ids": [DELIVERED_ID]}),
            "kind",
        ),
        (
            _report(root_cause={"kind": "service", "evidence_ids": [DELIVERED_ID]}),
            "name",
        ),
        (
            _report(
                root_cause={"kind": "service", "name": "payment", "evidence_ids": []}
            ),
            "evidence_ids",
        ),
    ],
)
def test_invalid_root_cause_shape_is_report_invalid_with_field_feedback(
    payload: dict[str, Any], feedback_token: str
) -> None:
    parsed, reason, feedback = _validated(payload)
    assert parsed is None
    assert reason == "REPORT_INVALID"
    assert feedback_token in feedback


def test_root_cause_unknown_evidence_is_report_invalid_and_names_unknown_id() -> None:
    payload = _report(root_cause=_root_cause(evidence_ids=["ev-missing"]))
    parsed, reason, feedback = _validated(payload)
    assert parsed is None
    assert reason == "REPORT_INVALID"
    assert "unknown_evidence_id" in feedback
    assert "ev-missing" in feedback


@pytest.mark.parametrize("root_cause", ["omitted", None])
def test_v3_report_without_root_cause_or_with_null_root_cause_is_accepted(
    root_cause: object,
) -> None:
    parsed, reason, feedback = _validated(_report(root_cause=root_cause))
    assert parsed is not None
    assert reason == ""
    assert feedback == ""


def test_inconclusive_report_may_carry_a_structured_root_cause() -> None:
    payload = _report(
        root_cause=_root_cause(namespace=None),
        conclusion="inconclusive",
        assessment_status="incomplete",
        gaps=["Synthetic trace source is unavailable."],
    )
    parsed, reason, feedback = _validated(payload)
    assert parsed is not None
    assert reason == ""
    assert feedback == ""


def test_v2_report_remains_accepted() -> None:
    parsed, reason = parse_report(
        json.dumps(_report(schema_version="m0-report-v2", root_cause="omitted")),
        finish_reason="stop",
    )
    assert parsed is not None
    assert reason == ""


def test_unknown_report_schema_version_is_rejected() -> None:
    parsed, reason = parse_report(
        json.dumps(_report(schema_version="m0-report-v99")),
        finish_reason="stop",
    )
    assert parsed is None
    assert reason == "REPORT_INVALID"


def _manifest(raw: dict[str, Any]) -> Manifest:
    stored = raw["capture"]["manifest"]
    return Manifest(
        round_id=stored["round_id"],
        dimensions={
            name: DimensionValue(item["kind"], item.get("value"), item.get("reason"))
            for name, item in stored["dimensions"].items()
        },
    )


@pytest.mark.parametrize(
    "summary_path",
    sorted(Path("docs/evidence/eval").glob("**/summary.json")),
    ids=lambda path: str(path),
)
def test_frozen_opspilot_eval_summary_v1_still_loads(
    summary_path: Path, tmp_path: Path
) -> None:
    """Load every committed old summary after restoring embedded deliveries.

    The repository freezes summary JSON but intentionally does not carry the
    private recording/ledger blobs referenced by some captures.  Delivery
    records are embedded in each summary, so the test rebuilds those exact
    bytes for the resolver and removes only those unavailable optional refs.
    """

    raw = json.loads(summary_path.read_bytes())
    assert raw["schema"] == "opspilot-eval-summary-1"
    capture = raw["capture"]
    blobs = {
        ref["path"]: canonical_bytes(capture["delivered"][evidence_id])
        for evidence_id, ref in capture.get("delivery_refs", {}).items()
    }
    capture["recording_ref"] = None
    capture["ledger_ref"] = None
    local_summary = tmp_path / "summary.json"
    local_summary.write_bytes(canonical_bytes(raw))

    def resolve(ref: object) -> bytes:
        return blobs[ref.path]  # type: ignore[attr-defined]

    loaded = load_summary(
        local_summary,
        manifest=_manifest(raw),
        resolve=resolve,
    )
    assert loaded.attempt.started is True
