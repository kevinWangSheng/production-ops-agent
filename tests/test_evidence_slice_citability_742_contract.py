"""Independent contract tests for #742 (decision B1-D14,
docs/tasks/2026-11-22-next-batches-b1-prereview.md): a ``read_evidence``
slice of a kept over-limit result is citable as fact only when its parent
tool's registration is.

``fetch_skill`` is registered ``citable_as_fact=False``
(``opspilot/tools/knowledge_fetch.py``); a slice of its kept over-limit
result must therefore say ``citable_as_fact: false``. A slice of an
ordinary citable tool's kept result stays ``true`` (regression guard).

Driven through the ``otel-demo-ext`` worker profile's real executor
factory (switch ``OPSPILOT_KNOWLEDGE_TOOL=1``), its real
``read_evidence`` route and an in-memory evidence store
(``MemoryEvidenceStore``). No PostgreSQL, no network, no model. Two seams
are replaced, both outside the behavior under test:

* the source backends: ``RoutedTransport.fetch`` answers the parent tool
  with a prepared multi-row body and delegates every other tool
  (``read_evidence`` included) to the real route. Today's knowledge backend
  returns one row per skill, so a sliceable over-limit ``fetch_skill``
  result needs a prepared body (issue #742: "depends on sizes");
* the tokenizer: one token per character, so the parent view is
  deterministically over its 25,000-token limit while each slice fits.

Written from the issue and B1-D14 without reading any fix.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from opspilot.tools import ToolRequest, evidence_read, otel_demo_ext
from opspilot.tools.executor import TransportResponse
from opspilot.tools.profiles import select_profile
from opspilot.tools.registry import canonical
from opspilot.web.evidence import MemoryEvidenceStore
from tests.m1_tool_support import FakeClock
from tests.test_m1_otel_demo_contract import TARGET_ID, _lease
from tests.test_tool_expansion_c2b_contract import (
    BACKEND_URLS,
    CATALOG_KEY,
    FRAME,
    LIVE_SWITCH,
    NOW,
    QUESTION,
    RETRIEVED,
    SWITCH,
    A,
    _catalog,
    _DsnStore,
    _require_tools,
    _with_facts,
    _worker_env,
)

ROWS = 30
ROW_CHARS = 1_500  # 30 rows ~ 45 KB: under fetch_skill's 64 KiB byte cap,
# over the 25,000-token view limit at one token per character.


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    for name in (SWITCH, LIVE_SWITCH, *BACKEND_URLS):
        monkeypatch.delenv(name, raising=False)
    placeholder = tmp_path / "synthetic-ca-placeholder.pem"
    placeholder.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C2B_TEST_PLACEHOLDER_CA", str(placeholder))
    # One token per character (see module docstring).
    monkeypatch.setattr(otel_demo_ext, "default_counter", lambda: len)


def _skill_body() -> TransportResponse:
    rows = [
        {
            "kind": "runbook",
            "skill_id": f"checkout-errors-part-{i}",
            "content": f"step {i}: " + "x" * ROW_CHARS,
        }
        for i in range(ROWS)
    ]
    return TransportResponse(
        body=canonical({"results": rows}).encode("utf-8"),
        data_as_of=RETRIEVED,
        row_unit="entries",
        backend_rows_returned=ROWS,
    )


def _services_body() -> TransportResponse:
    services = [f"service-{i}-" + "s" * ROW_CHARS for i in range(ROWS)]
    return TransportResponse(body=canonical({"services": services}).encode("utf-8"))


PARENTS: dict[str, tuple[Any, dict[str, Any]]] = {
    "fetch_skill": (_skill_body, {"skill_id": "checkout-errors"}),
    "traces_list_services": (_services_body, {}),
}


def _executor(monkeypatch, parent: str):
    _require_tools()
    prepare, _ = PARENTS[parent]
    real_fetch = evidence_read.RoutedTransport.fetch

    def fetch(self, request):
        if request.tool == parent:
            return prepare()
        return real_fetch(self, request)

    monkeypatch.setattr(evidence_read.RoutedTransport, "fetch", fetch)
    clock = FakeClock(start=NOW)
    profile = select_profile(_worker_env())
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    store = _DsnStore(lease, deadline=deadline)
    sink = MemoryEvidenceStore()
    factory = profile.executor_factory(store, sink, clock)
    built = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question=QUESTION,
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    built = _with_facts(built, **{CATALOG_KEY: _catalog(A)})
    return factory(lease, built), sink


def _call(executor, index: int, tool: str, params: dict[str, Any]):
    return executor.execute(
        ToolRequest(
            step_id=f"742-step-{index}",
            tool_index=index,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=params,
            window=FRAME.as_json(),
        )
    )


def _kept_parent_then_slice(monkeypatch, parent: str):
    executor, sink = _executor(monkeypatch, parent)
    _, params = PARENTS[parent]
    kept = _call(executor, 0, parent, params)
    view = kept.model_view
    assert view is not None
    # Precondition: the parent result was kept over the limit, not adopted.
    assert (kept.status, kept.reason) == ("error", "RESULT_TOO_LARGE"), view
    assert view.get("adopted") is False and view.get("citable_as_fact") is False
    parent_id = view.get("evidence_id")
    assert isinstance(parent_id, str) and parent_id, view
    sliced = _call(
        executor,
        1,
        "read_evidence",
        {"evidence_id": parent_id, "offset": 0, "limit": 2},
    )
    slice_view = sliced.model_view
    assert slice_view is not None
    # Precondition: the slice itself was read and adopted.
    assert sliced.status == "ok", slice_view
    assert slice_view.get("adopted") is True, slice_view
    assert slice_view.get("parent_tool") == parent, slice_view
    assert sliced.evidence is not None
    stored = sink.get(sliced.evidence.evidence_id)
    assert stored is not None
    return slice_view, stored.view


def test_a_slice_of_a_kept_fetch_skill_result_is_not_citable_as_fact(monkeypatch):
    model_view, stored_view = _kept_parent_then_slice(monkeypatch, "fetch_skill")
    # What the model sees and what the report's citation check later reads
    # from the evidence store both carry the parent's registration.
    assert model_view["citable_as_fact"] is False, model_view
    assert stored_view["citable_as_fact"] is False, stored_view


def test_a_slice_of_a_kept_citable_tool_result_stays_citable_as_fact(monkeypatch):
    model_view, stored_view = _kept_parent_then_slice(
        monkeypatch, "traces_list_services"
    )
    assert model_view["citable_as_fact"] is True, model_view
    assert stored_view["citable_as_fact"] is True, stored_view
