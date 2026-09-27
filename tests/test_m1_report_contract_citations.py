"""Citation contract after the v4 2+2 packet (0/4 pass, three REPORT_INVALID).

``docs/evidence/m1-01-v4-acceptance/run.md`` and each Run's
``handoff-cause.md`` locate the three handoffs in
``opspilot.investigation.reports.unsupported_citations``:

- normal-2 and fault-1: a ``fact`` claim cited a delivered view whose
  ``status`` is ``no_data``. The model saw ``adopted: true`` plus an
  ``evidence_id`` on that view and nothing in the L2 contract told it that
  only ``ok`` views can carry a fact.
- fault-2: every claim cited the view's ``operation_id`` (``<step>-tN``), a
  strict prefix of the ``evidence_id`` (``<step>-tN:<uuid>``).

The user decided the rule stays as it is and the model-visible surface gets
clearer instead: the L2 contract names the rule, every delivered view carries
a ``citable_as_fact`` flag, the projection revision moves, and the packet's
three recorded reports keep failing the bind exactly as they did.

Tests A and B are RED on the pre-fix tree and GREEN once that lands; the
replay tests (D) are GREEN both before and after, guarding against anyone
loosening the rule to make the packet pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from opspilot.investigation.context import (
    context_policy_revision,
    delivered_view,
    view_stub,
)
from opspilot.investigation.loop import prompt_revision_versions
from opspilot.investigation.reports import (
    REPORT_CONTRACT,
    DeliveredView,
    context_target_catalog,
    context_time_policy_ids,
    parse_report,
    unsupported_citations,
)
from opspilot.tools import PROJECTION_REVISION, TransportResponse
from tests.m1_tool_support import body, build, request

# Pre-fix literals, pinned from the packet's own ledgers
# (docs/evidence/m1-01-v4-acceptance/<run>/ledger.json, runs[0].versions).
# A change to the model-visible contract must move these; a tree that still
# reports them has not changed what the model sees.
PRE_FIX_PROMPT_REVISION = "prompt-replay-candidate-2c26fd0db1e0"
PRE_FIX_CONTEXT_POLICY_REVISION = "ctx-ctx-policy-v1-156004a224ba"
PRE_FIX_PROJECTION_REVISION = "m1-01-tool-view-v4"

PACKET = (
    Path(__file__).resolve().parent.parent / "docs" / "evidence" / "m1-01-v4-acceptance"
)
AUTHORIZED = frozenset({"m0-otel-20260909"})
FACT_LIKE = frozenset({"fact", "counter_evidence", "rejected_hypothesis"})


# -- A. L2 report contract text -------------------------------------------


def test_contract_says_only_ok_views_support_facts_and_no_data_only_gaps():
    """normal-2 / fault-1 cause: the contract never told the model that a
    ``no_data`` view cannot support a fact (fault-1 handoff-cause, P3-1)."""
    text = REPORT_CONTRACT
    assert "no_data" in text
    # The word ``ok`` must appear as a status value, next to ``status``.
    assert re.search(r"status[^.]{0,80}\bok\b|\bok\b[^.]{0,80}status", text), (
        "REPORT_CONTRACT must say fact-like claims cite only views whose status is ok"
    )
    # Every fact-like kind is named where the rule is stated.
    for kind in ("fact", "counter_evidence", "rejected_hypothesis"):
        assert kind in text
    # no_data views support gaps/unknowns, not facts.
    assert re.search(r"no_data[^.]*\b(gap|gaps|unknown)", text) or re.search(
        r"\b(gap|gaps|unknown)[^.]*no_data", text
    ), "REPORT_CONTRACT must say no_data views may only support gaps/unknowns"


def test_contract_names_the_citable_as_fact_flag():
    """Each delivered view carries ``citable_as_fact``; the contract must
    point the model at it as the one field that decides fact citations."""
    assert "citable_as_fact" in REPORT_CONTRACT


def test_contract_forbids_citing_operation_id():
    """fault-2 cause: every claim cited ``operation_id`` (a strict prefix of
    ``evidence_id``); the contract must name that field and forbid it."""
    text = REPORT_CONTRACT
    assert "operation_id" in text
    assert "evidence_id" in text
    assert re.search(
        r"operation_id[^.]*\b(not|never)\b|\b(not|never)\b[^.]*operation_id", text
    ), "REPORT_CONTRACT must say operation_id is never an evidence_id / cited"


def test_prompt_revision_moved_with_the_contract_text():
    """C3 §5: the prompt revision is a hash of the L2 bytes, so a changed
    contract must not reclaim Runs recorded under the pre-fix literal."""
    assert prompt_revision_versions()["prompt_revision"] != PRE_FIX_PROMPT_REVISION


# -- B. Tool view fields ------------------------------------------------


def test_ok_view_is_citable_as_fact():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"metric": "x", "value": 1}]))

    outcome = executor.execute(request())

    assert outcome.status == "ok" and outcome.adopted
    assert outcome.model_view["citable_as_fact"] is True
    assert outcome.evidence.view["citable_as_fact"] is True


def test_no_data_view_is_adopted_but_not_citable_as_fact():
    """normal-2 / fault-1 cause: ``no_data`` is adopted evidence with an
    ``evidence_id``, which the model read as citable. The view itself must
    now say it is not."""
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]))

    outcome = executor.execute(request())

    assert outcome.status == "no_data" and outcome.adopted
    assert outcome.model_view["adopted"] is True
    assert outcome.model_view["citable_as_fact"] is False
    assert outcome.evidence.view["citable_as_fact"] is False


def test_denied_undeclared_parameter_view_is_not_citable_as_fact():
    executor, _, _, _ = build()

    outcome = executor.execute(request(params={"expr": "rate(x[5m])", "bogus": 1}))

    assert (outcome.status, outcome.reason) == ("denied", "PARAM_NOT_ALLOWED")
    assert outcome.model_view["citable_as_fact"] is False


def test_source_error_view_is_not_citable_as_fact():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]), source_status="400")

    outcome = executor.execute(request())

    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.adopted is False
    assert outcome.model_view["citable_as_fact"] is False


@pytest.mark.parametrize(
    ("rows", "expected"),
    [([{"value": 1}], True), ([], False)],
)
def test_citable_as_fact_is_a_bool_equal_to_ok_and_adopted(rows, expected):
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body(rows))

    view = executor.execute(request()).model_view

    assert type(view["citable_as_fact"]) is bool
    assert view["citable_as_fact"] is (view["status"] == "ok" and view["adopted"])
    assert view["citable_as_fact"] is expected


def test_projection_revision_is_v5():
    """The view gained a field, so the projection revision must move from
    the packet's recorded ``m1-01-tool-view-v4``."""
    assert PROJECTION_REVISION != PRE_FIX_PROJECTION_REVISION
    assert PROJECTION_REVISION == "m1-01-tool-view-v5"
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]))
    view = executor.execute(request()).model_view
    assert view["projection_revision"] == "m1-01-tool-view-v5"


def test_view_stub_keeps_citable_as_fact():
    """An oversized view is replaced by a stub; the citation flag is
    provenance and must survive, or the model loses the rule exactly when
    the view is largest."""
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"metric": "x", "value": 1}]))
    view = executor.execute(request()).model_view

    stub = view_stub(view, preview_chars=16)

    assert "citable_as_fact" in stub
    assert stub["citable_as_fact"] is True
    assert stub["evidence_id"] == view["evidence_id"]


def test_context_policy_revision_moved_with_the_stub_shape():
    assert context_policy_revision() != PRE_FIX_CONTEXT_POLICY_REVISION


# -- D. Packet replay: the rule is not loosened ---------------------------


def _replay(run: str):
    """Rebuild the citation check from the packet's persisted material.

    ``report.json`` keeps the raw model text; ``ledger.json`` keeps every
    persisted view and the v4 evidence context the Run was given.
    """
    folder = PACKET / run
    report_text = json.loads((folder / "report.json").read_text())["report_text"]
    ledger = json.loads((folder / "ledger.json").read_text())
    context = ledger["runs"][0]["input"]["evidence_context"]
    report, reason = parse_report(report_text, finish_reason="stop")
    assert report is not None, reason
    raw_views = [entry["view"] for entry in ledger["evidence"]]
    delivered = [
        view
        for view in (
            delivered_view(raw, evidence_context=context, authorized_targets=AUTHORIZED)
            for raw in raw_views
        )
        if view is not None
    ]
    assert delivered, run
    assert all(isinstance(view, DeliveredView) for view in delivered)
    return report, raw_views, delivered, context


def _unsupported(report, delivered, context) -> bool:
    return unsupported_citations(
        report,
        views=delivered,
        authorized_targets=AUTHORIZED,
        time_policy_ids=context_time_policy_ids(context),
        target_catalog=context_target_catalog(context, authorized_targets=AUTHORIZED),
    )


@pytest.mark.parametrize("run", ["normal-2", "fault-1", "fault-2"])
def test_packet_reports_still_fail_the_citation_bind(run):
    """Passes before and after the fix. The three recorded handoff reports
    must keep being rejected: the user's decision is to make the rule
    visible to the model, not to loosen it so the packet retroactively
    passes."""
    report, _, delivered, context = _replay(run)
    assert _unsupported(report, delivered, context) is True


@pytest.mark.parametrize("run", ["normal-2", "fault-1"])
def test_packet_fact_claims_cite_no_data_views(run):
    """Passes before and after the fix. Pins the specific cause the
    handoff-cause notes recorded: a fact-like claim cites a delivered view
    whose status is ``no_data``."""
    report, _, delivered, _ = _replay(run)
    by_id = {view.evidence_id: view for view in delivered}
    offending = [
        claim
        for claim in report.claims
        if claim.kind in FACT_LIKE
        and any(
            eid in by_id and by_id[eid].status == "no_data"
            for eid in claim.evidence_ids
        )
    ]
    assert offending, f"{run}: expected a fact-like claim citing a no_data view"
    # And it is that, not a fabricated id: every cited id was delivered.
    assert all(eid in by_id for claim in report.claims for eid in claim.evidence_ids)


def test_packet_fault_2_claims_cite_operation_ids_not_evidence_ids():
    """Passes before and after the fix. fault-2's claims cite ids that are
    not delivered ``evidence_id``s but are the ``operation_id`` of a
    delivered view, i.e. the strict prefix before ``:<uuid>``."""
    report, raw_views, delivered, _ = _replay("fault-2")
    delivered_ids = {view.evidence_id for view in delivered}
    operation_ids = {
        raw["operation_id"]: raw["evidence_id"]
        for raw in raw_views
        if raw.get("adopted") is True
    }
    # The prefix relationship the model exploited.
    for operation_id, evidence_id in operation_ids.items():
        assert evidence_id.startswith(operation_id + ":")
    cited = [eid for claim in report.claims for eid in claim.evidence_ids]
    assert cited
    assert not any(eid in delivered_ids for eid in cited)
    assert all(eid in operation_ids for eid in cited)
