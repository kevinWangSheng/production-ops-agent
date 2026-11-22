"""Implementer unit test for #742: a ``read_evidence`` slice is citable only
when its parent tool's registration is (B1-D14). No PostgreSQL."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opspilot.tools.evidence_read import (
    EvidenceReadTransport,
    RoutedTransport,
    evidence_registration,
)
from opspilot.tools.executor import EVIDENCE_TOOL, TransportResponse
from tests.m1_tool_support import (
    FakeTransport,
    RecordingSink,
    body,
    build,
    registration,
    request,
)


class _ReadableSink(RecordingSink):
    def count_spilled(self, run_id: str) -> tuple[int, int]:
        return len(self.records), sum(len(record.raw) for record in self.records)

    def get(self, evidence_id: str) -> Any:
        for record in self.records:
            if record.evidence_id == evidence_id:
                return SimpleNamespace(
                    evidence_id=record.evidence_id,
                    run_id="run-9",
                    raw=record.raw,
                    raw_sha256=record.raw_sha256,
                    view=record.view,
                    view_sha256=record.view_sha256,
                    adopted=record.adopted,
                    status=record.status,
                )
        return None


@pytest.mark.parametrize("parent_citable", [True, False])
def test_a_slice_takes_the_parent_tools_citability(parent_citable: bool) -> None:
    # The whole view does not fit ``max_view_tokens``, so the result is kept
    # (#308) and one row of it is read back.
    parent = registration(
        citable_as_fact=parent_citable, max_result_bytes=65536, max_view_tokens=256
    )
    reader = evidence_registration(max_window_seconds=3600)
    sink = _ReadableSink()
    sources = FakeTransport(
        response=TransportResponse(
            body=body([{"metric": f"m{i}", "value": "x" * 64} for i in range(200)])
        )
    )
    transport = RoutedTransport(
        evidence=EvidenceReadTransport(
            store=sink,
            run_id="run-9",
            target_ids=frozenset({"checkout-prod"}),
            result_paths={parent.name: parent.result_path},
            citable={parent.name: parent.citable_as_fact},
        ),
        sources=sources,
    )
    executor, _, _, _ = build(
        registrations=[parent, reader],
        transport=transport,
        sink=sink,
        scope_overrides={"tool_names": frozenset({parent.name, EVIDENCE_TOOL})},
    )
    kept = executor.execute(request())
    assert kept.model_view["reason"] == "RESULT_TOO_LARGE"
    sliced = executor.execute(
        request(
            step_id="step-2",
            tool_name=EVIDENCE_TOOL,
            params={"evidence_id": sink.records[-1].evidence_id, "limit": 1},
        )
    )
    assert sliced.status == "ok"
    assert sliced.model_view["citable_as_fact"] is parent_citable
