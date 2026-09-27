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
    delivered_from_context,
    parse_report,
    unsupported_citations,
)
from opspilot.tools import PROJECTION_REVISION, TransportResponse
from tests.m1_tool_support import body, build, historical_window_context, request

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


# -- C. The checker enforces citable_as_fact, not just status == ok ---------
#
# PR #56 review P2 (adopted): the L2 contract tells the model a fact may cite
# only a view whose ``citable_as_fact`` is true, so the checker must enforce
# that same flag. ``DeliveredView`` carries it (fail-closed default False),
# ``delivered_view`` copies it from the persisted view (deriving
# ``status == "ok"`` only when the key is absent), and
# ``delivered_from_context`` honours an explicit ``citable_as_fact: false``
# on a status-ok binding. Non-fact kinds are unaffected by the flag.

FIXTURE_RUN_ID = "run-9"
FIXTURE_TARGET = "checkout-prod"
FIXTURE_TARGETS = frozenset({FIXTURE_TARGET})
POLICY = "policy-window-1"


def _claim(evidence_id: str, *, kind: str = "fact") -> dict:
    """A fully scoped claim: valid target_ref and time_scope_ref, so the only
    thing left for the checker to reject is the cited view's flag."""
    return {
        "kind": kind,
        "text": f"{kind} citing {evidence_id}",
        "evidence_ids": [evidence_id],
        "target_refs": [FIXTURE_TARGET],
        "time_scope_ref": POLICY,
    }


def _report(*claims: dict):
    conclusion = "supported" if any(c["kind"] == "fact" for c in claims) else "partial"
    report, reason = parse_report(
        json.dumps(
            {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": conclusion,
                "summary": "Citation flag test.",
                "claims": list(claims),
                "gaps": [],
                "next_steps": [],
            }
        ),
        finish_reason="stop",
    )
    assert report is not None, reason
    return report


def _rejected(report, views) -> bool:
    return unsupported_citations(
        report,
        views=views,
        authorized_targets=FIXTURE_TARGETS,
        time_policy_ids=(POLICY,),
        target_catalog=None,
    )


def _ok_view(evidence_id: str, *, citable: bool) -> DeliveredView:
    return DeliveredView(
        evidence_id=evidence_id,
        target_ids=FIXTURE_TARGETS,
        status="ok",
        time_scope_refs=frozenset({POLICY}),
        citable_as_fact=citable,
    )


def test_delivered_view_citable_as_fact_defaults_to_false():
    """Fail-closed: a DeliveredView built without the flag is not fact
    evidence even when its status is ok."""
    view = DeliveredView(
        evidence_id="ev-default",
        target_ids=FIXTURE_TARGETS,
        status="ok",
        time_scope_refs=frozenset({POLICY}),
    )
    assert view.citable_as_fact is False
    assert _rejected(_report(_claim("ev-default")), [view]) is True


@pytest.mark.parametrize("kind", sorted(FACT_LIKE))
def test_ok_view_with_citable_false_cannot_support_a_fact_like_claim(kind):
    """The same status-ok view: flag False -> rejected, flag True -> accepted.
    Everything else about the claim is valid, so the flag is the only cause."""
    report = _report(_claim("ev-1", kind=kind))

    assert _rejected(report, [_ok_view("ev-1", citable=False)]) is True
    assert _rejected(report, [_ok_view("ev-1", citable=True)]) is False


def test_one_non_citable_view_among_several_rejects_the_claim():
    report = _report({**_claim("ev-good"), "evidence_ids": ["ev-good", "ev-flagged"]})
    views = [_ok_view("ev-good", citable=True), _ok_view("ev-flagged", citable=False)]
    assert _rejected(report, views) is True


def test_hypothesis_may_cite_a_non_citable_view():
    """Non-fact kinds are unaffected by the flag: a hypothesis binding to a
    delivered view that is not citable_as_fact still passes."""
    report = _report(_claim("ev-1", kind="hypothesis"))
    assert _rejected(report, [_ok_view("ev-1", citable=False)]) is False


def _persisted_ok_view() -> dict:
    """A real persisted view from the fixture executor (status ok, adopted,
    citable_as_fact True under projection v5)."""
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"metric": "x", "value": 1}]))
    outcome = executor.execute(request())
    raw = dict(outcome.evidence.view)
    assert raw["status"] == "ok" and raw["adopted"] is True
    assert raw["citable_as_fact"] is True
    return raw


def _through_delivered_view(raw: dict) -> DeliveredView:
    view = delivered_view(
        raw,
        evidence_context=historical_window_context(FIXTURE_RUN_ID),
        authorized_targets=FIXTURE_TARGETS,
    )
    assert view is not None
    assert view.status == "ok"
    assert POLICY in view.time_scope_refs, "fixture view must bind the policy"
    return view


def test_delivered_view_carries_citable_false_from_the_persisted_view():
    raw = {**_persisted_ok_view(), "citable_as_fact": False}

    view = _through_delivered_view(raw)

    assert view.citable_as_fact is False
    assert _rejected(_report(_claim(view.evidence_id)), [view]) is True
    # Non-fact kinds are unaffected.
    assert _rejected(_report(_claim(view.evidence_id, kind="hypothesis")), [view]) is (
        False
    )


def test_delivered_view_carries_citable_true_from_the_persisted_view():
    raw = {**_persisted_ok_view(), "citable_as_fact": True}

    view = _through_delivered_view(raw)

    assert view.citable_as_fact is True
    assert _rejected(_report(_claim(view.evidence_id)), [view]) is False


def test_delivered_view_without_the_key_derives_citable_from_status_ok():
    """A view lacking the key (older projection) derives ``status == "ok"``
    rather than the fail-closed dataclass default."""
    raw = _persisted_ok_view()
    del raw["citable_as_fact"]

    view = _through_delivered_view(raw)

    assert view.citable_as_fact is True
    assert _rejected(_report(_claim(view.evidence_id)), [view]) is False


def _v4_context(evidence_id: str, **binding_extra) -> dict:
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": FIXTURE_RUN_ID,
        "time_policies": [{"id": POLICY}],
        "view_bindings": {
            evidence_id: {
                "status": "ok",
                "target_refs": [FIXTURE_TARGET],
                "time_scope_refs": [POLICY],
                **binding_extra,
            }
        },
    }


def test_delivered_from_context_honours_explicit_citable_false():
    context = _v4_context("ev-bound", citable_as_fact=False)

    views = delivered_from_context(context, run_id=FIXTURE_RUN_ID)

    assert [view.evidence_id for view in views] == ["ev-bound"]
    (view,) = views
    assert view.status == "ok"
    assert view.citable_as_fact is False
    assert _rejected(_report(_claim("ev-bound")), views) is True
    # Non-fact kinds are unaffected.
    assert _rejected(_report(_claim("ev-bound", kind="hypothesis")), views) is False


def test_delivered_from_context_without_the_key_keeps_ok_views_citable():
    """Existing behaviour: a status-ok binding with no flag is citable."""
    context = _v4_context("ev-bound")

    views = delivered_from_context(context, run_id=FIXTURE_RUN_ID)

    assert [view.evidence_id for view in views] == ["ev-bound"]
    assert views[0].citable_as_fact is True
    assert _rejected(_report(_claim("ev-bound")), views) is False


def test_delivered_from_context_explicit_citable_true_is_citable():
    context = _v4_context("ev-bound", citable_as_fact=True)

    views = delivered_from_context(context, run_id=FIXTURE_RUN_ID)

    assert views[0].citable_as_fact is True
    assert _rejected(_report(_claim("ev-bound")), views) is False


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
